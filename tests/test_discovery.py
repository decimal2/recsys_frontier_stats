"""Agent behavior tests with synthetic webpages and mocked official API responses."""
import copy
import io
import json
import os
import tempfile
import unittest
import urllib.error
import xml.etree.ElementTree as ET
from argparse import Namespace
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from test_pipeline import ROOT, feed
from paper_pipeline.arxiv import NS, parse_atom, timestamp
from paper_pipeline.cli import load_config, parser
from paper_pipeline.discovery import SearchAgent, TOOLS, discover, web_url
from paper_pipeline.providers import OFFICIAL, OfficialAPIError, OfficialClient
from paper_pipeline.storage import connect, save_paper, start_run

URL = "https://arxiv.org/abs/2601.00001"
BLOG = "https://example.org/research"


def candidate(identifier="2601.00001", source=URL):
    return {"arxiv_id": identifier, "source_url": source, "bucket": "recsys",
            "priority": 5, "reason": "Synthetic evidence for a user-item modeling method"}


def call(name, args, identifier="call-1"):
    return {"role": "assistant", "content": None, "reasoning_content": "synthetic-private-reasoning",
            "tool_calls": [{"id": identifier, "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)}}]}


def turn(name, args, identifier="call-1"):
    return call(name, args, identifier), {"usage": {"total_tokens": 7}}


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.config = load_config(ROOT / "pipeline_config.json")
        self.agent = SearchAgent(self.config, "deepseek", self.directory)
        self.since = timestamp("2026-10-22T00:00:00Z")
        self.until = timestamp("2026-10-23T00:00:00Z")

    def test_model_reads_webpage_then_changes_query_and_submits_observed_paper(self):
        turns = [turn("web_search", {"query": "industrial recsys"}, "search-1"),
                 turn("web_fetch", {"url": BLOG}, "fetch-1"),
                 turn("web_search", {"query": "latent user item architecture"}, "search-2"),
                 turn("submit_papers", {"summary": "Found via research blog", "papers": [candidate(source=BLOG)]})]
        snapshots = []
        def completion(messages, tools, max_tokens):
            snapshots.append(copy.deepcopy(messages))
            return turns.pop(0)
        with patch.object(self.agent.client, "tools", side_effect=completion), \
             patch.object(self.agent.web, "search", side_effect=[[{"url": BLOG, "summary": "Research list"}], []]) as search, \
             patch.object(self.agent.web, "fetch", return_value={"content": "Synthetic paper " + URL}) as fetch:
            result = self.agent.run(self.since, self.until, 10)
        self.assertEqual(result["papers"][0]["arxiv_id"], "2601.00001")
        self.assertEqual(search.call_args_list[1].args[0], "latent user item architecture")
        self.assertIn(URL, snapshots[2][-1]["content"])
        self.assertEqual(snapshots[1][2]["reasoning_content"], "synthetic-private-reasoning")
        fetch.assert_called_once_with(BLOG)
        trace = (self.directory / "agent-trace.json").read_text()
        self.assertNotIn("synthetic-private-reasoning", trace)
        self.assertEqual(json.loads(trace)["status"], "completed")
        self.assertEqual(len(json.loads(trace)["rounds"]), 4)

    def test_invalid_submission_returns_error_then_model_can_recover(self):
        turns = [turn("submit_papers", {"summary": "Unsupported", "papers": [candidate()]}),
                 turn("web_search", {"query": "recsys"}), turn("web_fetch", {"url": URL}),
                 turn("submit_papers", {"summary": "Supported", "papers": [candidate()]})]
        with patch.object(self.agent.client, "tools", side_effect=turns), \
             patch.object(self.agent.web, "search", return_value=[{"url": URL}]), \
             patch.object(self.agent.web, "fetch", return_value={"content": "Synthetic primary paper"}), \
             self.assertLogs("paper_pipeline.discovery", level="WARNING"):
            result = self.agent.run(self.since, self.until, 10)
        self.assertEqual(len(result["papers"]), 1)
        trace = json.loads((self.directory / "agent-trace.json").read_text())
        self.assertIn("error", trace["tools"][0]["result"])

    def test_unobserved_paper_or_mismatched_source_cannot_be_submitted(self):
        self.agent.searches = self.agent.fetches = 1
        self.agent.observe(URL, BLOG)
        for paper in (candidate("2610.99999"), candidate(source="https://example.org/fake")):
            with self.subTest(paper=paper), self.assertRaisesRegex(ValueError, "tool-observed"):
                self.agent.submit({"summary": "Test", "papers": [paper]})

    def test_budget_and_duplicate_request_cache(self):
        self.agent.settings["max_tool_calls"] = 2
        with patch.object(self.agent.web, "search", return_value=[]) as search:
            self.agent.execute("web_search", {"query": "one"})
            self.assertTrue(self.agent.execute("web_search", {"query": "one"})["cached"])
            self.agent.execute("web_search", {"query": "two"})
            with self.assertRaisesRegex(ValueError, "budget"):
                self.agent.execute("web_search", {"query": "three"})
        self.assertEqual(search.call_count, 2)
        self.assertEqual(self.agent.submit({"summary": "No matches", "papers": []})["papers"], [])

    def test_round_limit_finalizes_from_evidence_when_last_reply_still_fetches(self):
        self.agent.settings["max_rounds"] = 3
        turns = [turn("web_search", {"query": "recsys"}), turn("web_fetch", {"url": BLOG}),
                 turn("web_fetch", {"url": URL})]
        selection = {"summary": "Selected from collected pages", "papers": [candidate()]}
        with patch.object(self.agent.client, "tools", side_effect=turns) as completion, \
             patch.object(self.agent.web, "search", return_value=[{"url": BLOG}]), \
             patch.object(self.agent.web, "fetch", return_value={"content": URL}), \
             patch.object(self.agent.client, "json", return_value=(selection, {"usage": {"total_tokens": 17}})) as finalize:
            result = self.agent.run(self.since, self.until, 10)
        self.assertEqual(result["papers"], [candidate()])
        self.assertEqual(completion.call_count, 3)
        finalize.assert_called_once()
        context = json.loads(finalize.call_args.args[1])
        self.assertIn("2601.00001", context["observed_sources"])
        self.assertEqual(len(context["evidence"]), 3)
        trace = json.loads((self.directory / "agent-trace.json").read_text())
        self.assertEqual(trace["status"], "completed")
        self.assertEqual(trace["finalization"][0]["invocation"]["usage"]["total_tokens"], 17)
        progress = json.loads(completion.call_args.args[0][-1]["content"])["agent_progress"]
        self.assertEqual(progress["rounds_left"], 0)

    def test_web_budget_transitions_to_finalization_without_another_search_turn(self):
        self.agent.settings["max_tool_calls"] = 2
        turns = [turn("web_search", {"query": "recsys"}), turn("web_fetch", {"url": URL})]
        selection = {"summary": "Enough evidence", "papers": [candidate()]}
        with patch.object(self.agent.client, "tools", side_effect=turns) as completion, \
             patch.object(self.agent.web, "search", return_value=[{"url": URL}]), \
             patch.object(self.agent.web, "fetch", return_value={"content": "Paper"}), \
             patch.object(self.agent.client, "json", return_value=(selection, {})) as finalize:
            self.assertEqual(len(self.agent.run(self.since, self.until, 10)["papers"]), 1)
        self.assertEqual(completion.call_count, 2)
        finalize.assert_called_once()

    def test_finalization_can_repair_invalid_source_once(self):
        self.agent.searches = self.agent.fetches = 1
        self.agent.observe(URL, BLOG)
        bad = {"summary": "Unsupported", "papers": [candidate("2610.99999")]}
        good = {"summary": "Supported", "papers": [candidate()]}
        with patch.object(self.agent.client, "json", side_effect=[(bad, {}), (good, {})]) as finalize, \
             self.assertLogs("paper_pipeline.discovery", level="WARNING"):
            result = self.agent.finalize({"target_papers": 10})
        self.assertEqual(result["papers"], [candidate()])
        self.assertEqual(finalize.call_count, 2)
        self.assertIn("2610.99999", finalize.call_args.args[1])

    def test_no_supported_submission_fails_after_bounded_finalization(self):
        self.agent.settings["max_rounds"] = 3
        with patch.object(self.agent.client, "tools", return_value=({"role": "assistant", "content": "done"}, {})) as completion, \
             patch.object(self.agent.client, "json", return_value=({"summary": "No search happened", "papers": []}, {})) as finalize, \
             self.assertLogs("paper_pipeline.discovery", level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "valid final selection"):
                self.agent.run(self.since, self.until, 10)
        self.assertEqual(completion.call_count, 3)
        self.assertEqual(finalize.call_count, 2)

    def test_auth_failure_stops_agent_and_trace_redacts_secret(self):
        secret = "synthetic-api-key-not-real"
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": secret}), \
             patch.object(self.agent.client, "tools", return_value=turn("web_search", {"query": "recsys"})) as completion, \
             patch.object(self.agent.web, "search", side_effect=OfficialAPIError("deepseek", 401, secret)), \
             self.assertLogs("paper_pipeline.discovery", level="ERROR"):
            with self.assertRaises(OfficialAPIError):
                self.agent.run(self.since, self.until, 10)
        completion.assert_called_once()
        trace = (self.directory / "agent-trace.json").read_text()
        self.assertNotIn(secret, trace)
        self.assertIn("[REDACTED]", trace)
        self.assertEqual(json.loads(trace)["status"], "failed")

    def test_fetch_failure_can_be_returned_to_model_for_recovery(self):
        turns = [turn("web_search", {"query": "recsys"}), turn("web_fetch", {"url": URL}),
                 turn("web_fetch", {"url": BLOG}),
                 turn("submit_papers", {"summary": "Recovered", "papers": [candidate(source=BLOG)]})]
        with patch.object(self.agent.client, "tools", side_effect=turns), \
             patch.object(self.agent.web, "search", return_value=[{"url": URL}]), \
             patch.object(self.agent.web, "fetch", side_effect=[OfficialAPIError("deepseek", 404, "Missing"), {"content": URL}]), \
             self.assertLogs("paper_pipeline.discovery", level="WARNING"):
            self.assertEqual(len(self.agent.run(self.since, self.until, 10)["papers"]), 1)

    def test_fetch_accepts_model_constructed_public_urls_and_keeps_address_validation(self):
        for url in ("file:///etc/passwd", "http://127.0.0.1/page", "https://user:pass@example.org/", "http://example.local/"):
            with self.subTest(url=url), patch.object(self.agent.web, "fetch") as fetch:
                with self.assertRaises(ValueError):
                    self.agent.execute("web_fetch", {"url": url})
                fetch.assert_not_called()
        urls = ["https://arxiv.org/list/cs.IR/recent?skip=50&show=50", URL]
        with patch.object(self.agent.web, "fetch", return_value={"content": "Synthetic public page"}) as fetch:
            for url in urls:
                self.assertEqual(self.agent.execute("web_fetch", {"url": url})["url"], url)
        self.assertEqual([c.args[0] for c in fetch.call_args_list], urls)
        self.assertIn(URL, self.agent.sources["2601.00001"])

    def test_escaped_markdown_identifiers_and_relative_paper_links_are_observed(self):
        source = "https://arxiv.org/list/cs.IR/recent"
        page = r"\- \[1\] arXiv\:2601\.00001v2 \[pdf\, html\]" + "\n[paper](/abs/2601.00002)"
        self.agent.observe(page, source)
        self.agent.searches = self.agent.fetches = 1
        papers = [candidate(source=source), candidate("2601.00002", source)]
        result = self.agent.submit({"summary": "Read the list", "papers": papers})
        self.assertEqual([p["arxiv_id"] for p in result["papers"]], ["2601.00001", "2601.00002"])

    def test_search_result_escaped_markdown_is_not_double_escaped_by_json(self):
        with patch.object(self.agent.web, "search", return_value=[{
                "url": BLOG, "summary": r"A paper arXiv\:2601\.00001"}]):
            self.agent.execute("web_search", {"query": "recsys"})
        self.assertIn(BLOG, self.agent.sources["2601.00001"])

    def test_fetch_403_is_recoverable_using_another_page(self):
        turns = [turn("web_search", {"query": "recsys"}), turn("web_fetch", {"url": URL}),
                 turn("web_fetch", {"url": BLOG}),
                 turn("submit_papers", {"summary": "Read alternative source", "papers": [candidate(source=BLOG)]})]
        with patch.object(self.agent.client, "tools", side_effect=turns), \
             patch.object(self.agent.web, "search", return_value=[{"url": URL}]), \
             patch.object(self.agent.web, "fetch", side_effect=[OfficialAPIError("deepseek", 403, "Page denied"), {"content": URL}]), \
             self.assertLogs("paper_pipeline.discovery", level="WARNING"):
            self.assertEqual(len(self.agent.run(self.since, self.until, 10)["papers"]), 1)

    def test_three_provider_tool_protocols_preserve_reasoning_and_tool_ids(self):
        for provider in OFFICIAL:
            with self.subTest(provider=provider):
                client = OfficialClient(self.config, provider)
                response = {"choices": [{"finish_reason": "tool_calls", "message": call("web_search", {"query": "recsys"})}]}
                with patch.object(client, "post", return_value=response) as post:
                    message, _ = client.tools([{"role": "user", "content": "Find papers"}], TOOLS, 8192)
                    messages = [{"role": "user", "content": "Find papers"}, message,
                                {"role": "tool", "tool_call_id": "call-1", "content": "[]"}]
                    client.tools(messages, TOOLS, 8192)
                payload = post.call_args.args[1]
                self.assertNotIn("response_format", payload)
                self.assertEqual(payload["messages"][1]["reasoning_content"], "synthetic-private-reasoning")
                self.assertEqual(payload["messages"][2]["tool_call_id"], "call-1")

    def test_incomplete_tool_response_is_not_executed(self):
        response = {"choices": [{"finish_reason": "length", "message": call("web_search", {"query": "recsys"})}]}
        with patch.object(self.agent.client, "post", return_value=response), self.assertRaisesRegex(ValueError, "incomplete"):
            self.agent.client.tools([], TOOLS, 8192)

    def discover_args(self):
        return Namespace(provider="deepseek", since="2026-10-22", until="2026-10-22",
                         db=self.directory / "papers.sqlite3", output_dir=self.directory / "reports")

    def test_primary_metadata_date_filtering_and_semantic_selection_of_unchanged_paper(self):
        raw = ET.fromstring(feed([0, 1]))
        entry = raw.find("a:entry", NS)
        entry.find("a:title", NS).text = "Synthetic Latent Paths"
        entry.find("a:summary", NS).text = "An alternative architecture with no old rule keywords."
        payload = ET.tostring(raw)
        papers, _, _ = parse_atom(payload, self.config)
        connection = connect(self.discover_args().db)
        self.addCleanup(connection.close)
        run_id = start_run(connection, "atom_import", self.config)
        rejected = {"selected": False, "bucket": "excluded", "score": 0, "tags": [],
                    "reasons": ["测试：此前未入选"], "matched_terms": [], "text_basis": "test_fixture"}
        with connection:
            save_paper(connection, run_id, papers[0], self.config, selection_override=rejected)
        self.assertEqual(connection.execute("SELECT count(*) FROM digest_queue").fetchone()[0], 0)
        result = {"summary": "Synthetic selection", "papers": [candidate(), candidate(papers[1]["arxiv_id"], papers[1]["url"])]}
        with patch("paper_pipeline.providers.OfficialClient.require_key", return_value="synthetic-key"), \
             patch("paper_pipeline.discovery.SearchAgent.run", return_value=result), \
             patch("paper_pipeline.discovery.ArxivClient.request", return_value=payload) as request:
            output = discover(self.discover_args(), self.config, connection)
        self.assertIn("id_list=", request.call_args.args[0])
        self.assertNotIn("search_query", request.call_args.args[0])
        self.assertEqual(output["outside_window"], [papers[1]["arxiv_id"]])
        self.assertEqual(output["unique_seen"], 1)
        row = connection.execute("SELECT metadata_json,selection_json FROM digest_queue").fetchone()
        self.assertEqual(json.loads(row[0])["title"], "Synthetic Latent Paths")
        self.assertEqual(json.loads(row[1])["method"], "agent")
        self.assertEqual(connection.execute("SELECT count(*) FROM provenance WHERE origin='agent_search'").fetchone()[0], 1)

    def test_metadata_batch_failure_does_not_discard_the_run(self):
        connection = connect(self.discover_args().db)
        self.addCleanup(connection.close)
        result = {"summary": "s", "papers": [candidate(), candidate("2601.00002", "https://arxiv.org/abs/2601.00002")]}
        error = urllib.error.HTTPError("https://export.arxiv.org", 429, "Too many", {}, None)
        with patch("paper_pipeline.providers.OfficialClient.require_key", return_value="synthetic-key"), \
             patch("paper_pipeline.discovery.SearchAgent.run", return_value=result), \
             patch("paper_pipeline.discovery.ArxivClient.request", side_effect=error):
            output = discover(self.discover_args(), self.config, connection)
        self.assertEqual(output["metadata_failed"], ["2601.00001", "2601.00002"])
        self.assertEqual(output["unique_seen"], 0)
        self.assertEqual(output["agent_candidates"], 2)
        self.assertEqual(output["metadata_verified"], 0)
        self.assertEqual(connection.execute("SELECT status FROM runs").fetchone()[0], "partial")
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM pending_metadata").fetchone()[0], 2)

    def test_old_rule_collection_cli_options_are_removed(self):
        for args in (["run"], ["reclassify"], ["daily", "--search"], ["discover", "--query", "fixed query"]):
            with self.subTest(args=args), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                parser().parse_args(args)
            self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
