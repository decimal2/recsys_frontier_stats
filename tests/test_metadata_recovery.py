"""Deferred arXiv metadata remains recoverable without paying for rediscovery."""
import io
import json
import tempfile
import unittest
import urllib.error
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from test_pipeline import ROOT, feed
from test_discovery import URL, candidate, turn
from paper_pipeline.arxiv import ArxivClient, iso, timestamp
from paper_pipeline.cli import load_config, main
from paper_pipeline.discovery import discover, import_pending_metadata, recover_pending_metadata
from paper_pipeline.runtime import get_task
from paper_pipeline.storage import connect, dumps, finish_run, pending_metadata, queue_metadata, start_run


class MetadataRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.config = load_config(ROOT / "pipeline_config.json")
        self.args = Namespace(provider="deepseek", since="2026-10-22", until="2026-10-22", limit=10,
                              db=self.directory / "papers.sqlite3", task_id="metadata-recovery", resume=False)
        self.connection = connect(self.args.db)
        self.addCleanup(self.connection.close)
        self.now = timestamp("2026-10-27T00:00:00Z")
        self.since = timestamp("2026-10-21T16:00:00Z")
        self.until = timestamp("2026-10-22T16:00:00Z")
        stub = patch("paper_pipeline.providers.OfficialClient.require_key", return_value="synthetic-key")
        stub.start()
        self.addCleanup(stub.stop)

    def fail_discovery(self, identifiers=("2601.00001",), headers=None):
        selected = {"summary": "Saved web evidence", "papers": [candidate(i) for i in identifiers]}
        error = urllib.error.HTTPError("https://export.arxiv.org/api/query", 429, "Too many", headers, None)
        with patch("paper_pipeline.discovery.utcnow", return_value=self.now), \
             patch("paper_pipeline.discovery.SearchAgent.run", return_value=selected), \
             patch("paper_pipeline.discovery.ArxivClient.request", side_effect=error), \
             self.assertLogs("paper_pipeline.discovery", level="WARNING"):
            return discover(self.args, self.config, self.connection)

    def test_429_persists_source_and_stats_then_retry_survives_restart_without_model(self):
        failed = self.fail_discovery(headers={"Retry-After": "900"})
        self.assertEqual((failed["agent_candidates"], failed["metadata_verified"], failed["status"]), (1, 0, "partial"))
        saved = pending_metadata(self.connection)[0]
        self.assertEqual(saved["decision"], candidate())
        self.assertEqual(saved["attempts"], 1)
        self.assertEqual(timestamp(saved["next_retry_at"]), self.now + timedelta(seconds=900))
        self.connection.close()
        self.connection = connect(self.args.db)
        self.addCleanup(self.connection.close)
        with patch("paper_pipeline.discovery.utcnow", return_value=self.now + timedelta(minutes=16)), \
             patch("paper_pipeline.discovery.SearchAgent.run", side_effect=AssertionError("No rediscovery")), \
             patch("paper_pipeline.discovery.ArxivClient.request", return_value=feed([0])) as request:
            recovered = recover_pending_metadata(self.args, self.config, self.connection, self.since, self.until)
        request.assert_called_once()
        self.assertEqual(recovered["attempted_ids"], ["2601.00001"])
        self.assertEqual(recovered["unique_seen"], 1)
        self.assertEqual(pending_metadata(self.connection), [])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM digest_queue").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT status FROM runs").fetchone()[0], "completed")
        evidence = json.loads(self.connection.execute("SELECT evidence_json FROM provenance WHERE origin='agent_search'").fetchone()[0])
        self.assertEqual(evidence["decision"], candidate())
        self.assertEqual(evidence["run_id"], failed["run_id"])

    def test_completed_discover_resume_retries_only_metadata_and_remembers_repaired_output(self):
        self.fail_discovery()
        self.args.resume = True
        with patch("paper_pipeline.discovery.utcnow", return_value=self.now), \
             patch("paper_pipeline.discovery.ArxivClient.request") as request, \
             patch("paper_pipeline.discovery.SearchAgent.run", side_effect=AssertionError("No rediscovery")):
            deferred = discover(self.args, self.config, self.connection)
        request.assert_not_called()
        self.assertEqual(deferred["metadata_recovery"]["status"], "deferred")
        with patch("paper_pipeline.discovery.utcnow", return_value=self.now + timedelta(minutes=6)), \
             patch("paper_pipeline.discovery.ArxivClient.request", return_value=feed([0])) as request, \
             patch("paper_pipeline.discovery.SearchAgent.run", side_effect=AssertionError("No rediscovery")):
            first = discover(self.args, self.config, self.connection)
            second = discover(self.args, self.config, self.connection)
        request.assert_called_once()
        self.assertEqual(first, second)
        self.assertEqual(first["metadata_failed"], [])
        self.assertEqual(first["metadata_verified"], 1)
        self.assertEqual(get_task(self.directory, self.args.task_id)["result"], first)

    def test_partial_atom_response_keeps_verified_paper_and_missing_id(self):
        selected = {"summary": "Two candidates", "papers": [candidate(), candidate("2601.00002")]}
        with patch("paper_pipeline.discovery.SearchAgent.run", return_value=selected), \
             patch("paper_pipeline.discovery.ArxivClient.request", return_value=feed([0])):
            result = discover(self.args, self.config, self.connection)
        self.assertEqual((result["agent_candidates"], result["metadata_verified"], result["unique_seen"]), (2, 1, 1))
        self.assertEqual(result["metadata_failed"], ["2601.00002"])
        self.assertEqual([row["arxiv_id"] for row in pending_metadata(self.connection)], ["2601.00002"])

    def test_explicit_cli_resume_repairs_completed_daily_child(self):
        self.args.discovery_task_id = "daily-parent-search-0"
        self.args.task_id = None
        self.fail_discovery()
        stdout = io.StringIO()
        with patch("paper_pipeline.discovery.utcnow", return_value=self.now + timedelta(minutes=6)), \
             patch("paper_pipeline.discovery.ArxivClient.request", return_value=feed([0])) as request, \
             patch("paper_pipeline.discovery.SearchAgent.run", side_effect=AssertionError("No rediscovery")), \
             patch("paper_pipeline.cli.LOG_ROOT", self.directory / "logs"), \
             redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            status = main(["--db", str(self.args.db), "resume", self.args.discovery_task_id])
        request.assert_called_once()
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(stdout.getvalue())["metadata_failed"], [])

    def test_interrupted_search_keeps_original_exclusions_when_pending_queue_changes(self):
        turns = [turn("web_search", {"query": "recsys"}), turn("web_fetch", {"url": URL})]
        with patch("paper_pipeline.providers.OfficialClient.tools", side_effect=turns), \
             patch("paper_pipeline.providers.DeepSeekSearch.search", return_value=[{"url": URL}]), \
             patch("paper_pipeline.providers.DeepSeekSearch.fetch", side_effect=RuntimeError("interrupted")), \
             self.assertLogs("paper_pipeline", level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                discover(self.args, self.config, self.connection)
        other_run = start_run(self.connection, "agent_search", self.config, self.since, self.until)
        with self.connection:
            queue_metadata(self.connection, other_run, candidate("2601.00002"), self.since, self.until)
        self.args.resume = True
        selection = {"summary": "Resumed saved search", "papers": [candidate()]}
        with patch("paper_pipeline.providers.OfficialClient.tools", return_value=turn("submit_papers", selection)), \
             patch("paper_pipeline.providers.DeepSeekSearch.search", side_effect=AssertionError("Search repeated")), \
             patch("paper_pipeline.providers.DeepSeekSearch.fetch", return_value={"content": "Synthetic paper"}), \
             patch("paper_pipeline.discovery.ArxivClient.request", return_value=feed([0])):
            result = discover(self.args, self.config, self.connection)
        self.assertEqual(result["unique_seen"], 1)

    def test_endpoint_cooldown_defers_new_ids_in_next_search_window(self):
        self.fail_discovery()
        self.args.task_id = "next-window"
        selected = {"summary": "New candidate", "papers": [candidate("2601.00002")]}
        with patch("paper_pipeline.discovery.utcnow", return_value=self.now + timedelta(seconds=1)), \
             patch("paper_pipeline.discovery.SearchAgent.run", return_value=selected) as search, \
             patch("paper_pipeline.discovery.ArxivClient.request") as request:
            result = discover(self.args, self.config, self.connection)
        request.assert_not_called()
        self.assertIn("2601.00001", search.call_args.kwargs["excluded_ids"])
        self.assertEqual(result["metadata_failed"], ["2601.00002"])
        self.assertEqual(pending_metadata(self.connection)[1]["attempts"], 0)

    def test_legacy_trace_import_is_idempotent_and_preserves_related_ir_candidate(self):
        run_id = start_run(self.connection, "agent_search", self.config, self.since, self.until)
        decision = {**candidate(), "bucket": "related_ir"}
        with self.connection:
            finish_run(self.connection, run_id, {"agent_candidates": 0, "metadata_failed": ["2601.00001"], "unique_seen": 0})
        folder = self.directory / "raw" / f"run-{run_id:06d}"
        folder.mkdir(parents=True)
        (folder / "agent-trace.json").write_text(dumps({"status": "completed", "finalization": [
            {"result": {"summary": "Observed", "papers": [decision]}}]}))
        self.assertEqual(import_pending_metadata(self.connection, self.directory), ["2601.00001"])
        self.assertEqual(import_pending_metadata(self.connection, self.directory), [])
        self.assertEqual(pending_metadata(self.connection)[0]["decision"], decision)
        with patch("paper_pipeline.discovery.ArxivClient.request") as request:
            result = recover_pending_metadata(self.args, self.config, self.connection, self.since, self.until,
                                              excluded_ids=["2601.00001"])
        request.assert_not_called()
        self.assertEqual(result["attempted_ids"], [])
        summary = json.loads(self.connection.execute("SELECT summary_json FROM runs").fetchone()[0])
        self.assertEqual(summary["agent_candidates"], 1)

    def test_recovered_metadata_still_obeys_date_window(self):
        run_id = start_run(self.connection, "agent_search", self.config, self.since, self.until)
        with self.connection:
            queue_metadata(self.connection, run_id, candidate(), self.since, self.until)
        with patch("paper_pipeline.discovery.ArxivClient.request", return_value=feed([0])):
            result = recover_pending_metadata(self.args, self.config, self.connection,
                                              self.since, timestamp("2026-10-22T00:00:00Z"))
        self.assertEqual(result["outside_window"], ["2601.00001"])
        self.assertEqual(result["unique_seen"], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 0)

    def test_metadata_client_429_defers_without_exponential_sleep(self):
        client = ArxivClient(self.config, self.directory / "network", metadata=True)
        error = urllib.error.HTTPError("https://export.arxiv.org", 429, "Too many", None, None)
        with patch("paper_pipeline.arxiv.urllib.request.urlopen", side_effect=error) as request, \
             patch("paper_pipeline.arxiv.time.sleep") as sleep:
            with self.assertRaises(urllib.error.HTTPError):
                client.request("https://export.arxiv.org/api/query?id_list=2601.00001")
        request.assert_called_once()
        self.assertTrue(all(call.args[0] <= 3.1 for call in sleep.call_args_list))

    def test_metadata_client_transport_retry_is_bounded(self):
        client = ArxivClient(self.config, self.directory / "network", metadata=True)
        with patch("paper_pipeline.arxiv.urllib.request.urlopen", side_effect=TimeoutError("timeout")) as request, \
             patch("paper_pipeline.arxiv.time.sleep"):
            with self.assertRaises(TimeoutError):
                client.request("https://export.arxiv.org/api/query?id_list=2601.00001")
        self.assertEqual(request.call_count, 2)


if __name__ == "__main__":
    unittest.main()
