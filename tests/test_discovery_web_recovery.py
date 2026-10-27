"""Offline regressions for per-page failure isolation and misleading HTTP 200 pages."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_discovery import BLOG, URL, candidate, turn
from test_pipeline import ROOT
from paper_pipeline.arxiv import timestamp
from paper_pipeline.cli import load_config
from paper_pipeline.discovery import SearchAgent
from paper_pipeline.providers import OfficialAPIError, OfficialTransportError
from paper_pipeline.web_evidence import FailedPages, PAGE_TTL, TRANSIENT_TTL, page_problem


class WebRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.config = load_config(ROOT / "pipeline_config.json")
        self.agent = SearchAgent(self.config, "deepseek", self.directory)
        self.agent.messages = None
        self.since = timestamp("2026-10-22T00:00:00Z")
        self.until = timestamp("2026-10-23T00:00:00Z")

    def step(self, agent, url=URL):
        call = turn("web_fetch", {"url": url})[0]["tool_calls"]
        return agent.tool_step(agent.snapshot({"round": 1, "pending": call, "tool_index": 0}))

    def test_504_allows_agent_to_change_source_and_submit(self):
        turns = [turn("web_search", {"query": "industrial recommendation"}),
                 turn("web_fetch", {"url": URL}), turn("web_fetch", {"url": BLOG}),
                 turn("submit_papers", {"summary": "Alternate source", "papers": [candidate(source=BLOG)]})]
        with patch.object(self.agent.client, "tools", side_effect=turns), \
             patch.object(self.agent.web, "search", return_value=[{"url": URL}]), \
             patch.object(self.agent.web, "fetch", side_effect=[OfficialAPIError("deepseek", 504, "Timed out"),
                                                              {"content": URL}]), \
             self.assertLogs("paper_pipeline.discovery", level="WARNING"):
            result = self.agent.run(self.since, self.until, 10)
        self.assertEqual(len(result["papers"]), 1)
        trace = json.loads((self.directory / "agent-trace.json").read_text())
        failed = trace["tools"][1]["result"]
        self.assertEqual(failed["http_status"], 504)
        self.assertTrue(failed["recoverable"])
        self.assertFalse(failed["usable"])
        self.assertEqual(trace["status"], "completed")

    def test_page_transient_errors_and_timeouts_are_recoverable(self):
        errors = [OfficialAPIError("deepseek", status, "Temporary") for status in (408, 429, 500, 502, 503, 504)]
        errors += [OfficialTransportError("Connection unavailable"), TimeoutError("Timed out"), ConnectionError("Reset")]
        for index, error in enumerate(errors):
            with self.subTest(error=error), \
                 patch.object(self.agent.web, "fetch", side_effect=error), \
                 self.assertLogs("paper_pipeline.discovery", level="WARNING"):
                state = self.step(self.agent, f"https://example.org/paper/{index}")
            self.assertTrue(state["trace"]["tools"][-1]["result"]["recoverable"])
            self.assertEqual(state["fetches"], 0)

    def test_provider_distinguishes_transport_exhaustion_from_network_permission(self):
        self.config["llm"]["retries"] = 0
        opener = Mock()
        with patch.object(self.agent.web.client, "require_key", return_value="synthetic-key"), \
             patch("paper_pipeline.providers.urllib.request.build_opener", return_value=opener), \
             self.assertLogs("paper_pipeline.providers", level="ERROR"):
            opener.open.side_effect = TimeoutError("Timeout")
            with self.assertRaises(OfficialTransportError):
                self.agent.web.fetch(URL)
            opener.open.side_effect = PermissionError(1, "blocked")
            with self.assertRaises(RuntimeError) as failure:
                self.agent.web.fetch(URL)
            self.assertNotIsInstance(failure.exception, OfficialTransportError)

    def test_auth_permissions_and_unknown_runtime_failures_stay_fatal_and_traced(self):
        secret = "synthetic-do-not-record"
        errors = [OfficialAPIError("deepseek", 401, secret), OfficialAPIError("deepseek", 402, "quota"),
                  RuntimeError("EPERM network permission denied"), RuntimeError("interrupted fetch")]
        for index, error in enumerate(errors):
            with self.subTest(error=error), patch.dict(os.environ, {"DEEPSEEK_API_KEY": secret}), \
                 patch.object(self.agent.web, "fetch", side_effect=error), \
                 self.assertLogs("paper_pipeline.discovery", level="ERROR"), self.assertRaises(type(error)):
                self.step(self.agent, f"https://example.org/fatal/{index}")
            trace_text = (self.directory / "agent-trace.json").read_text()
            self.assertNotIn(secret, trace_text)
            self.assertFalse(json.loads(trace_text)["tools"][-1]["result"]["recoverable"])
        self.assertEqual(self.agent.failed_pages.load(), {})

    def test_failure_cache_is_shared_across_windows_expires_and_keeps_other_urls_available(self):
        first_dir = self.directory / "raw" / "run-000001"
        next_dir = self.directory / "raw" / "run-000002"
        first_dir.mkdir(parents=True)
        next_dir.mkdir()
        first = SearchAgent(self.config, "deepseek", first_dir)
        first.messages = None
        with patch("paper_pipeline.web_evidence.time.time", return_value=1000), \
             patch.object(first.web, "fetch", side_effect=OfficialAPIError("deepseek", 504, "Timeout")), \
             self.assertLogs("paper_pipeline.discovery", level="WARNING"):
            self.step(first)
        second = SearchAgent(self.config, "deepseek", next_dir)
        with patch("paper_pipeline.web_evidence.time.time", return_value=1001), patch.object(second.web, "fetch") as fetch:
            result = second.execute("web_fetch", {"url": URL})
            fetch.assert_not_called()
            self.assertTrue(result["cached"])
            self.assertEqual(second.calls, 0)
            self.assertEqual(second.failed_pages.hints()[0]["url"], URL)
        alternative = URL + "v2"
        with patch.object(second.web, "fetch", return_value={"content": "Paper"}) as fetch:
            second.execute("web_fetch", {"url": alternative})
            fetch.assert_called_once_with(alternative)
        with patch("paper_pipeline.web_evidence.time.time", return_value=1001 + TRANSIENT_TTL), \
             patch.object(second.web, "fetch", return_value={"content": "Recovered paper"}) as fetch:
            self.assertTrue(second.execute("web_fetch", {"url": URL})["usable"])
            fetch.assert_called_once_with(URL)

    def test_explicit_bad_pages_are_labeled_without_counting_as_read_evidence(self):
        pages = [("subscription_preview", r"Title arXiv\:2601\.00001\n\#\# 本期为订阅内容"),
                 ("search_form_error", r"Whoops\! Something went wrong\. Please correct errors in the form below\."),
                 ("empty_listing", r"No updates for this time period\.")]
        for index, (status, content) in enumerate(pages):
            url = f"https://example.org/preview/{index}"
            with self.subTest(status=status), patch.object(self.agent.web, "fetch", return_value={"content": content}), \
                 self.assertLogs("paper_pipeline.discovery", level="WARNING"):
                result = self.agent.execute("web_fetch", {"url": url})
            self.assertFalse(result["usable"])
            self.assertEqual(result["page_status"], status)
            self.assertIn("next_action", result)
        self.assertEqual(self.agent.fetches, 0)
        self.assertEqual(self.agent.sources, {})
        self.agent.searches = 1
        with self.assertRaisesRegex(ValueError, "read at least one"):
            self.agent.submit({"summary": "Preview only", "papers": [candidate()]})
        self.assertIsNone(page_problem("Complete research paper. Subscribe to our newsletter."))

    def test_failure_memory_is_bounded_and_does_not_store_page_content(self):
        memory = FailedPages(self.directory / "failed.json")
        with patch("paper_pipeline.web_evidence.MAX_FAILURES", 2), \
             patch("paper_pipeline.web_evidence.time.time", return_value=1000):
            for index in range(3):
                memory.remember(f"https://example.org/{index}", {"usable": False, "content": "body"}, PAGE_TTL)
            self.assertEqual(set(memory.load()), {"https://example.org/1", "https://example.org/2"})
            self.assertNotIn("content", memory.get("https://example.org/2"))


if __name__ == "__main__":
    unittest.main()
