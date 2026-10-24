"""Behavior tests: local imports, version safety, retries and restart safety."""
import copy
import io
import json
import sys
import tempfile
import unittest
import urllib.error
import xml.etree.ElementTree as ET
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from paper_pipeline.arxiv import ArxivClient, NS, arxiv_id, parse_atom, timestamp
from paper_pipeline.cli import load_config, main
from paper_pipeline.discovery import agent_selection
from paper_pipeline.storage import connect, history_paper, run_summary, save_paper, start_run

FIXTURE = ROOT / "tests" / "fixtures" / "arxiv_sample.xml"
PAYLOAD = FIXTURE.read_bytes()


def feed(indexes, start=0, total=4):
    original = ET.fromstring(PAYLOAD)
    entries = original.findall("a:entry", NS)
    for entry in entries:
        original.remove(entry)
    for index in indexes:
        original.append(entries[index])
    original.find("os:startIndex", NS).text = str(start)
    original.find("os:totalResults", NS).text = str(total)
    return ET.tostring(original)


def selection(paper, config, selected=True):
    if not selected:
        return {"selected": False, "bucket": "excluded", "score": 0, "tags": [],
                "reasons": ["测试：未命中选题"], "matched_terms": [], "text_basis": "test_fixture"}
    return agent_selection(paper, {"arxiv_id": paper["arxiv_id"], "source_url": paper["url"],
                                   "bucket": "recsys", "priority": 5, "reason": "测试入选"}, config)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.config = load_config(ROOT / "pipeline_config.json")
        self.db = self.directory / "papers.sqlite3"
        self.reports = self.directory / "reports"
        log_patch = patch("paper_pipeline.cli.LOG_ROOT", self.directory / "logs")
        log_patch.start()
        self.addCleanup(log_patch.stop)

    def tearDown(self):
        self.temporary.cleanup()

    def cli(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = main(["--db", str(self.db), "--output-dir", str(self.reports), *arguments])
        return status, json.loads(stdout.getvalue()) if stdout.getvalue() else {}, stderr.getvalue()

    def test_identifiers_and_parser_preserve_versions_and_affiliations(self):
        self.assertEqual(arxiv_id("https://arxiv.org/pdf/2601.00001v2.pdf"), ("2601.00001", 2))
        self.assertEqual(arxiv_id("cs/9901002v3"), ("cs/9901002", 3))
        with self.assertRaises(ValueError):
            arxiv_id("https://example.org/abs/2601.00001")
        papers, total, start = parse_atom(PAYLOAD, self.config)
        self.assertEqual((len(papers), total, start), (4, 4, 0))
        self.assertEqual(papers[0]["companies"], ["Kuaishou"])
        self.assertNotIn("Amazon", papers[0]["companies"])  # Dataset is not affiliation evidence.
        self.assertEqual(papers[1]["company_evidence_kind"], "unknown")

    def test_api_error_and_html_are_not_empty_success(self):
        error = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/api/errors#bad_query</id><summary>Bad query</summary></entry></feed>'
        with self.assertRaisesRegex(ValueError, "Bad query"):
            parse_atom(error, self.config)
        with self.assertRaises(ValueError):
            parse_atom(b"<html>Maintenance</html>", self.config)

    def test_agent_selection_uses_agent_bucket_and_priority_without_keyword_bonus(self):
        papers, _, _ = parse_atom(PAYLOAD, self.config)
        paper = papers[0]  # companies=["Kuaishou"], evidence from the arXiv affiliation field.
        decision = {"arxiv_id": paper["arxiv_id"], "source_url": paper["url"],
                    "bucket": "recsys", "priority": 5, "reason": "测试入选"}
        selected = agent_selection(paper, decision, self.config)
        self.assertEqual(selected["score"], 110)
        self.assertEqual(selected["team_priority"], "preferred")
        self.assertEqual(selected["preferred_companies"], ["Kuaishou"])
        self.assertEqual(selected["method"], "agent")
        self.assertEqual(selected["tags"], [])
        unknown = agent_selection({**paper, "companies": []}, decision, self.config)
        self.assertEqual(unknown["team_priority"], "unknown")
        self.assertEqual(unknown["score"], selected["score"])  # No programmatic company bonus.
        other = agent_selection(paper, {**decision, "bucket": "llm_for_ir", "priority": 1}, self.config)
        self.assertLess(other["score"], selected["score"])  # recsys outranks adjacent topics.

    def test_primary_metadata_keeps_labeled_history_company_claim(self):
        papers, _, _ = parse_atom(PAYLOAD, self.config)
        paper = papers[1]  # No affiliation in the API fixture.
        history = history_paper({"url": paper["url"], "title": paper["title"], "date": "2026-10-22",
                                 "companies": ["Meta"], "page_url": "https://example.org/history",
                                 "evidence": "Secondary-source company claim"})
        connection = connect(self.db)
        try:
            run_id = start_run(connection, "history", self.config)
            save_paper(connection, run_id, history, self.config, selection_override=selection(history, self.config))
            save_paper(connection, run_id, paper, self.config, selection_override=selection(paper, self.config))
            stored = json.loads(connection.execute("SELECT metadata_json FROM papers").fetchone()[0])
            self.assertEqual(stored["origin"], "arxiv")
            self.assertEqual(stored["abstract"], paper["abstract"])
            self.assertEqual(stored["companies"], ["Meta"])
            self.assertEqual(stored["company_evidence_kind"], "history_report")
            self.assertEqual(stored["company_source_url"], "https://example.org/history")
            raw_evidence = json.loads(connection.execute("SELECT evidence_json FROM provenance WHERE origin='arxiv'").fetchone()[0])
            self.assertEqual(raw_evidence["company_evidence_kind"], "unknown")
        finally:
            connection.close()





    def test_http_retry_honors_retry_after_and_400_not_retried(self):
        client = ArxivClient(self.config, self.directory / "raw")
        error = urllib.error.HTTPError("https://export.arxiv.org", 429, "Too many", {"Retry-After": "90"}, None)
        response = io.BytesIO(PAYLOAD)
        with patch("paper_pipeline.arxiv.urllib.request.urlopen", side_effect=[error, response]) as opener, \
             patch("paper_pipeline.arxiv.time.sleep") as sleep:
            self.assertEqual(client.request("https://export.arxiv.org/api/query"), PAYLOAD)
            self.assertEqual(opener.call_count, 2)
            self.assertIn(90, [call.args[0] for call in sleep.call_args_list])
        throttled = urllib.error.HTTPError("https://export.arxiv.org", 429, "Too many", {}, None)
        with patch("paper_pipeline.arxiv.urllib.request.urlopen", side_effect=[throttled, io.BytesIO(PAYLOAD)]), \
             patch("paper_pipeline.arxiv.time.sleep") as sleep:
            client.request("https://export.arxiv.org/api/query")
            self.assertIn(30.0, [call.args[0] for call in sleep.call_args_list])  # 429 backs off longer.
        bad = urllib.error.HTTPError("https://export.arxiv.org", 400, "Bad request", {}, None)
        with patch("paper_pipeline.arxiv.urllib.request.urlopen", side_effect=bad) as opener, \
             patch("paper_pipeline.arxiv.time.sleep"):
            with self.assertRaises(urllib.error.HTTPError):
                client.request("https://export.arxiv.org/api/query")
            self.assertEqual(opener.call_count, 1)

    def test_offline_ingest_idempotence(self):
        papers, _, _ = parse_atom(PAYLOAD, self.config)
        connection = connect(self.db)
        try:
            for expected in ({"new": 4}, {"unchanged": 4}):
                run_id = start_run(connection, "atom_import", self.config)
                with connection:
                    for paper in papers:
                        save_paper(connection, run_id, paper, self.config,
                                   selection_override=selection(paper, self.config))
                summary = run_summary(connection, run_id)
                self.assertEqual(summary["actions"], expected)
            self.assertEqual(summary["selected_seen"], 4)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 4)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM digest_queue").fetchone()[0], 4)
        finally:
            connection.close()

    def test_versions_cannot_downgrade_and_history_cannot_replace_arxiv(self):
        papers, _, _ = parse_atom(PAYLOAD, self.config)
        connection = connect(self.db)
        try:
            run_id = start_run(connection, "atom_import", self.config)
            paper = papers[0]
            save_paper(connection, run_id, paper, self.config, selection_override=selection(paper, self.config))
            lower = {**paper, "version": 1, "title": "Older title"}
            save_paper(connection, run_id, lower, self.config, selection_override=selection(lower, self.config))
            history = {**paper, "origin": "recsys_frontier", "title": "Unverified title", "source_date": "2026-10-22"}
            save_paper(connection, run_id, history, self.config, selection_override=selection(history, self.config))
            stored = json.loads(connection.execute("SELECT metadata_json FROM papers").fetchone()[0])
            self.assertEqual(stored["title"], paper["title"])
            self.assertEqual(stored["version"], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM provenance").fetchone()[0], 2)
        finally:
            connection.close()

    def test_arxiv_permission_error_is_not_retried(self):
        client = ArxivClient(self.config, self.directory / "raw")
        client.last_request = None
        with patch("paper_pipeline.arxiv.urllib.request.urlopen", side_effect=urllib.error.URLError(PermissionError(1, "blocked"))) as request, \
             patch("paper_pipeline.arxiv.time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "EPERM/EACCES"):
                client.request("https://export.arxiv.org/api/query?id_list=2305.05065")
            request.assert_called_once()
            sleep.assert_not_called()

    def test_rate_limit_config_is_enforced(self):
        self.config["arxiv"]["request_interval_seconds"] = 0.1
        path = self.directory / "config.json"
        path.write_text(json.dumps(self.config))
        with self.assertRaisesRegex(ValueError, ">= 3"):
            load_config(path)

    def test_daily_invalid_output_budget_fails_before_collection(self):
        self.config["llm"]["providers"]["deepseek"]["max_output"] = 393216
        self.config["llm"]["max_tokens"] = 786432
        path = self.directory / "config.json"
        path.write_text(json.dumps(self.config))
        with patch("paper_pipeline.discovery.SearchAgent.run") as collect:
            status, _, error = self.cli("--config", str(path), "daily", "--provider", "deepseek")
        self.assertEqual(status, 1)
        self.assertIn("786432", error)
        self.assertIn("393216", error)
        collect.assert_not_called()

    def test_daily_invalid_date_window_fails_before_network(self):
        with patch("paper_pipeline.providers.OfficialClient.require_key", return_value="test-fake-key"), \
             patch("paper_pipeline.discovery.SearchAgent.run") as collect:
            status, _, error = self.cli("daily", "--since", "2026-10-23", "--until", "2026-10-21")
        self.assertEqual(status, 1)
        self.assertIn("--since must be earlier", error)
        collect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
