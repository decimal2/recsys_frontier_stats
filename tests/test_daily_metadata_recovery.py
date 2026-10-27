"""Daily integration for deferred metadata, using offline backfill fixtures."""
import io
import json
import unittest
import urllib.parse
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import test_backfill as backfill
from test_pipeline import selection
from paper_pipeline.arxiv import parse_atom, timestamp
from paper_pipeline.daily import digest
from paper_pipeline.cli import main
from paper_pipeline.storage import finish_run, queue_metadata, run_summary, save_paper, start_run


class DailyMetadataRecoveryTests(unittest.TestCase):
    def setUp(self):
        # Compose the shared fixture without rediscovering its inherited tests.
        self.fixture = backfill.BackfillTests(methodName="runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def ingest(self, identifiers):
        fixture = self.fixture
        url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(
            {"id_list": ",".join(identifiers)})
        papers, _, _ = parse_atom(fixture.metadata(url), fixture.config)
        run_id = start_run(fixture.connection, "metadata_retry", fixture.config)
        with fixture.connection:
            for paper in papers:
                save_paper(fixture.connection, run_id, paper, fixture.config,
                           selection_override=selection(paper, fixture.config))
            finish_run(fixture.connection, run_id, run_summary(fixture.connection, run_id))

    def recover(self, identifiers=(), failed=()):
        def operation(*args, **kwargs):
            if identifiers:
                self.ingest(identifiers)
            return {"attempted_ids": list(identifiers) + list(failed),
                    "metadata_failed": list(failed), "pending_ids": list(failed)}
        return patch("paper_pipeline.discovery.recover_pending_metadata",
                     side_effect=operation, create=True)

    def test_recovered_queue_fills_minimum_without_a_new_search(self):
        fixture = self.fixture
        identifiers = [fixture.paper(index) for index in range(5)]
        with self.recover(identifiers) as recovery:
            result = digest(fixture.args, fixture.config, fixture.connection)
        self.assertTrue(result["webpage_ready"])
        self.assertEqual(result["coverage"]["actual"], 5)
        self.assertEqual(set(fixture.analyzed), set(identifiers))
        self.assertEqual(fixture.searches, [])
        recovery.assert_called_once()
        self.assertEqual(recovery.call_args.args[3], timestamp("2026-09-22T16:00:00Z"))
        self.assertEqual(recovery.call_args.args[4], timestamp("2026-10-22T16:00:00Z"))

    def test_recovered_older_papers_wait_for_their_window_and_future_paper_stays_out(self):
        fixture = self.fixture
        identifiers = [fixture.paper(index) for index in range(3)]
        identifiers.extend([fixture.paper(3, "2026-10-10"), fixture.paper(4, "2026-09-28")])
        future = fixture.paper(5, "2026-10-23")
        with self.recover(identifiers + [future]) as recovery:
            result = digest(fixture.args, fixture.config, fixture.connection)
        self.assertTrue(result["webpage_ready"])
        self.assertEqual(set(fixture.analyzed), set(identifiers))
        self.assertNotIn(future, fixture.analyzed)
        self.assertEqual(timestamp(result["paper_window"]["since_utc"]), timestamp("2026-09-22T16:00:00Z"))
        self.assertEqual(timestamp(result["paper_window"]["until_utc_exclusive"]),
                         timestamp("2026-10-22T16:00:00Z"))
        self.assertLessEqual(len(fixture.searches), 3)
        recovery.assert_called_once()

    def test_deferred_failure_does_not_stop_fresh_search_or_repeat_recovery(self):
        fixture = self.fixture
        deferred = fixture.paper(9)
        fresh = [fixture.paper(index) for index in range(5)]
        fixture.batches = [fresh]
        with self.recover(failed=[deferred]) as recovery:
            result = digest(fixture.args, fixture.config, fixture.connection)
        self.assertEqual(result["coverage"]["actual"], 5)
        self.assertEqual(set(fixture.analyzed), set(fresh))
        self.assertEqual(len(fixture.searches), 1)
        self.assertEqual(result["metadata_recovery"]["metadata_failed"], [deferred])
        self.assertEqual(result["metadata_recovery"]["pending_ids"], [deferred])
        recovery.assert_called_once()

    def test_rejected_older_candidate_still_allows_search_in_the_final_window(self):
        fixture = self.fixture
        recent = [fixture.paper(index) for index in range(4)]
        school = fixture.paper(4, "2026-09-28", company=False)
        fresh = fixture.paper(5, "2026-09-28")
        fixture.batches = [[], [], [fresh]]

        def search_after_queued_work(since, until, target, excluded_ids=None):
            if len(fixture.searches) == 2:
                self.assertIn(school, fixture.analyzed)
                self.assertIn(school, excluded_ids)
            return fixture.search(since, until, target, excluded_ids)

        with self.recover(recent + [school]) as recovery, \
             patch("paper_pipeline.discovery.SearchAgent.run", side_effect=search_after_queued_work):
            result = digest(fixture.args, fixture.config, fixture.connection)
        self.assertTrue(result["webpage_ready"])
        self.assertEqual(result["coverage"]["actual"], 5)
        self.assertEqual(len(fixture.searches), 3)
        self.assertEqual(fixture.searches[-1]["since"], timestamp("2026-09-22T16:00:00Z"))
        payload = json.loads(Path(result["json"]).read_text())
        self.assertEqual({item["paper"]["arxiv_id"] for item in payload["items"]}, set(recent + [fresh]))
        self.assertIn(school, {item["arxiv_id"] for item in payload["rejected"]})
        recovery.assert_called_once()

    def test_completed_shortfall_resume_does_not_repeat_metadata_or_model_calls(self):
        fixture = self.fixture
        recovered, deferred = fixture.paper(0), fixture.paper(1)
        with self.recover([recovered], [deferred]) as recovery:
            first = digest(fixture.args, fixture.config, fixture.connection)
        self.assertFalse(first["webpage_ready"])
        self.assertEqual(first["coverage"]["actual"], 1)
        self.assertEqual(len(fixture.searches), 3)
        recovery.assert_called_once()
        before = Path(first["json"]).read_bytes()
        fixture.args.task_id, fixture.args.resume = first["task_id"], True
        with patch("paper_pipeline.discovery.recover_pending_metadata", create=True,
                   side_effect=AssertionError("completed recovery repeated")), \
             patch("paper_pipeline.discovery.SearchAgent.run",
                   side_effect=AssertionError("completed search repeated")), \
             patch("paper_pipeline.daily.analyze",
                   side_effect=AssertionError("completed insight repeated")):
            resumed = digest(fixture.args, fixture.config, fixture.connection)
        self.assertEqual(resumed, first)
        self.assertEqual(Path(first["json"]).read_bytes(), before)

    def test_other_edition_and_rejected_ids_are_excluded_from_recovery(self):
        fixture = self.fixture
        fixture.config["digest"]["min_papers"] = 1
        published = fixture.paper(0)
        with self.recover([published]):
            first = digest(fixture.args, fixture.config, fixture.connection)
        first_bytes = Path(first["json"]).read_bytes()
        rejected = fixture.paper(1, company=False)
        with fixture.connection:
            # Edition exclusion still applies if the processed marker is absent.
            fixture.connection.execute("DELETE FROM digest_processed WHERE arxiv_id=?", (published,))
            fixture.connection.execute("INSERT INTO digest_processed VALUES (?,?,?,?)",
                                       (rejected, 1, "out_of_scope", "2026-10-22T12:00:00Z"))
        fixture.args.date = fixture.args.since = fixture.args.until = "2026-10-23"
        fresh = fixture.paper(2, "2026-10-22")
        with self.recover([published, fresh]) as recovery:
            second = digest(fixture.args, fixture.config, fixture.connection)
        self.assertTrue({published, rejected} <= recovery.call_args.kwargs["excluded_ids"])
        payload = json.loads(Path(second["json"]).read_text())
        self.assertEqual([item["paper"]["arxiv_id"] for item in payload["items"]], [fresh])
        self.assertEqual(fixture.analyzed.count(published), 1)
        self.assertNotIn(rejected, fixture.analyzed)
        self.assertEqual(Path(first["json"]).read_bytes(), first_bytes)

    def test_plain_digest_does_not_retry_network_metadata(self):
        fixture = self.fixture
        fixture.args.command = "digest"
        with patch("paper_pipeline.discovery.recover_pending_metadata", create=True,
                   side_effect=AssertionError("digest must not fetch metadata")):
            result = digest(fixture.args, fixture.config, fixture.connection)
        self.assertEqual(result["coverage"]["actual"], 0)
        self.assertEqual(fixture.searches, [])

    def test_pending_command_exposes_metadata_evidence_without_network(self):
        fixture = self.fixture
        identifier = fixture.paper(0)
        run_id = start_run(fixture.connection, "agent_search", fixture.config)
        decision = {"arxiv_id": identifier, "source_url": "https://arxiv.org/abs/" + identifier,
                    "bucket": "recsys", "priority": 5, "reason": "Saved industrial evidence"}
        with fixture.connection:
            queue_metadata(fixture.connection, run_id, decision,
                           timestamp("2026-10-15T16:00:00Z"), timestamp("2026-10-22T16:00:00Z"))
        output = io.StringIO()
        with patch("paper_pipeline.cli.LOG_ROOT", fixture.directory / "logs"), \
             patch("paper_pipeline.discovery.ArxivClient.request", side_effect=AssertionError("No network")), \
             patch("paper_pipeline.discovery.SearchAgent.run", side_effect=AssertionError("No model")), \
             redirect_stdout(output), redirect_stderr(io.StringIO()):
            code = main(["--db", str(fixture.args.db), "pending"])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["pending"], [])
        self.assertEqual(result["pending_metadata"][0]["decision"], decision)
        self.assertIn("last_error", result["pending_metadata"][0])
        self.assertIn("next_retry_at", result["pending_metadata"][0])


if __name__ == "__main__":
    unittest.main()
