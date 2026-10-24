import copy
import hashlib
import http.client
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_pipeline import selection
from paper_pipeline.arxiv import date_boundary, parse_atom, timestamp
from paper_pipeline.cli import load_config, main
from paper_pipeline.daily import digest, in_window, queued
from paper_pipeline.discovery import discover
from paper_pipeline.fulltext import extract_html, extract_pdf
from paper_pipeline.insights import analyze, synthesize, validate_analysis, validate_synthesis
from paper_pipeline.providers import DeepSeekSearch, NoRedirect, OFFICIAL, OfficialClient, load_env
from paper_pipeline.publishing import rebuild_reports, render_daily
from paper_pipeline.selection import organization_details
from paper_pipeline.storage import connect, save_paper, start_run


def document(identifier="2601.00001", version=2):
    text = "Kuaishou Technology. Our recommender uses semantic IDs. Recall improves by 12% on Dataset A."
    return {"arxiv_id": identifier, "version": version, "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "content_chars": len(text), "source_url": "https://arxiv.org/pdf/" + identifier,
            "note": "Synthetic fixture text, not a real paper.", "format": "pdf", "page_count": 1,
            "blocks": [{"id": "p001b0001", "section": "Page 1", "page": 1, "text": text}]}


def analysis():
    return {"title_zh": "合成测试：语义标识推荐", "one_liner": "合成测试摘要。", "in_scope": True,
        "relevance_reason": "推荐系统", "rating": 4, "recommendation_reason": "合成测试理由。",
        "problem": "合成测试问题。", "method": "用语义标识表示物品。", "method_evidence_ids": ["E1"],
        "key_results": [{"finding": "Dataset A 上 Recall 提升 12%。", "evidence_id": "E1"}],
        "insights": [{"idea": "可比较语义标识设计的影响。", "is_inference": True, "evidence_ids": ["E1"]}],
        "limitations": ["这是测试数据，不是研究结论。"],
        "affiliations": [{"organization": "Kuaishou Technology", "evidence_id": "E2"}],
        "evidence": [{"id": "E1", "block_id": "p001b0001", "quote": "Our recommender uses semantic IDs."},
                     {"id": "E2", "block_id": "p001b0001", "quote": "Kuaishou Technology"}]}


class FakeClient:
    calls = []
    fail_synthesis = False

    def __init__(self, config, provider=None):
        self.provider = provider or "deepseek"
        self.profile = config["llm"]["providers"][self.provider]
        self.model = self.profile["model"]
        self.settings = config["llm"]

    def require_key(self):
        return "fake-for-tests"

    def json(self, system, user):
        self.calls.append(user)
        if user.startswith("["):
            if self.fail_synthesis:
                raise RuntimeError("Synthetic synthesis outage")
            entries = json.loads(user.split("\n修正校验错误：")[0])
            ids = [entry["arxiv_id"] for entry in entries]
            result = {"trends": [], "topic": {"title": "测试专题", "analysis": "测试分析推断", "paper_ids": ids}}
        else:
            result = analysis()
        return result, {"provider": self.provider, "model": self.model, "usage": {"total_tokens": 99}}


class FakeFullText:
    def __init__(self, *args):
        pass

    def get(self, paper):
        return document(paper["arxiv_id"], paper["version"])


class InsightTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.config = load_config(ROOT / "pipeline_config.json")
        # Small fixtures exercise insight mechanics; quota behavior is tested in test_backfill.
        self.config["digest"]["min_papers"] = 1
        FakeClient.calls, FakeClient.fail_synthesis = [], False

    def tearDown(self):
        self.temporary.cleanup()

    def seed(self):
        connection = connect(self.directory / "papers.sqlite3")
        papers, _, _ = parse_atom((ROOT / "tests/fixtures/arxiv_sample.xml").read_bytes(), self.config)
        run_id = start_run(connection, "atom_import", self.config)
        with connection:
            for index, paper in enumerate(papers):
                save_paper(connection, run_id, paper, self.config,
                           selection_override=selection(paper, self.config, selected=index != 2))
        args = Namespace(provider="deepseek", date="2026-10-23", run_id=None, limit=2,
                         db=self.directory / "papers.sqlite3", output_dir=self.directory / "reports")
        return connection, papers, args

    def test_each_official_provider_uses_its_endpoint_and_json_contract(self):
        for provider, (url, key_env) in OFFICIAL.items():
            with self.subTest(provider=provider), patch.dict(os.environ, {key_env: "test-fake-key"}):
                response = io.BytesIO(json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": '{"ok": true}'}}], "usage": {"total_tokens": 5}}).encode())
                opener = Mock()
                opener.open.return_value = response
                with patch("paper_pipeline.providers.urllib.request.build_opener", return_value=opener):
                    result, meta = OfficialClient(self.config, provider).json("Return JSON", "test")
                request = opener.open.call_args.args[0]
                self.assertEqual(request.full_url, url + "/chat/completions")
                self.assertEqual(json.loads(request.data)["response_format"], {"type": "json_object"})
                self.assertEqual(meta["provider"], provider)
                self.assertTrue(result["ok"])

    def test_missing_key_and_redirect_do_not_send_requests(self):
        with patch.dict(os.environ, {}, clear=True), patch("paper_pipeline.providers.urllib.request.build_opener") as opener:
            with self.assertRaisesRegex(RuntimeError, "DEEPSEEK_API_KEY"):
                OfficialClient(self.config).post("/chat/completions", {})
            opener.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, "redirected"):
            NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.org")

    def test_truncated_completion_is_rejected(self):
        client = OfficialClient(self.config)
        with patch.object(client, "post", return_value={"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}):
            with self.assertRaisesRegex(ValueError, "incomplete"):
                client.json("JSON", "test")

    def test_deepseek_output_limit_is_checked_before_network(self):
        client = OfficialClient(self.config, "deepseek")
        self.config["llm"]["providers"]["deepseek"]["max_output"] = 393216
        self.config["llm"]["max_tokens"] = 393216
        client.validate_generation()
        for value in (393217, 786432):
            self.config["llm"]["max_tokens"] = value
            with patch.object(client, "post") as post:
                with self.assertRaisesRegex(ValueError, "393216"):
                    client.json("JSON", "test")
                post.assert_not_called()

    def test_http_error_preserves_server_reason_but_redacts_credentials(self):
        secret = "fake-sensitive-http-error-key"
        body = {"error": {"message": "max_tokens must be <= 393216; credential=" + secret,
                          "type": "invalid_request_error", "code": "invalid_parameter", "param": "max_tokens",
                          "echoed_request": "private-prompt-not-for-logs"},
                "request": "private-prompt-not-for-logs"}
        error = urllib.error.HTTPError("https://api.deepseek.com/chat/completions", 400, "Bad Request",
                                       {"x-request-id": "test-request-id"}, io.BytesIO(json.dumps(body).encode()))
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": secret}), \
             patch("paper_pipeline.providers.urllib.request.build_opener") as build, \
             self.assertLogs("paper_pipeline.providers", level="ERROR") as logs:
            build.return_value.open.side_effect = error
            with self.assertRaisesRegex(RuntimeError, "max_tokens must be <= 393216") as raised:
                OfficialClient(self.config, "deepseek").json("JSON", "private-prompt-not-for-logs")
        for text in (str(raised.exception), "\n".join(logs.output)):
            self.assertIn("invalid_parameter", text)
            self.assertIn("test-request-id", text)
            self.assertIn("[REDACTED]", text)
            self.assertNotIn(secret, text)
            self.assertNotIn("private-prompt-not-for-logs", text)
        build.return_value.open.assert_called_once()

    def test_incomplete_read_is_retried(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({"choices": [{"finish_reason": "stop",
                                                "message": {"content": "{}"}}]}).encode()

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-fake-key"}), \
             patch("paper_pipeline.providers.urllib.request.build_opener") as build, \
             patch("paper_pipeline.providers.time.sleep") as sleep:
            build.return_value.open.side_effect = [http.client.IncompleteRead(b""), Response()]
            client = OfficialClient(self.config, "deepseek")
            with self.assertLogs("paper_pipeline.providers", level="WARNING"):
                result = client.post("/chat/completions", {})
        self.assertEqual(result["choices"][0]["finish_reason"], "stop")
        self.assertEqual(build.return_value.open.call_count, 2)
        self.assertEqual(sleep.call_count, 1)

    def test_non_json_error_body_does_not_hide_http_status(self):
        error = urllib.error.HTTPError("https://api.deepseek.com/chat/completions", 400, "Bad Request",
                                       {}, io.BytesIO(b"<html>private-proxy-body</html>"))
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-fake-key"}), \
             patch("paper_pipeline.providers.urllib.request.build_opener") as build, \
             self.assertLogs("paper_pipeline.providers", level="ERROR"):
            build.return_value.open.side_effect = error
            with self.assertRaisesRegex(RuntimeError, "HTTP 400") as raised:
                OfficialClient(self.config, "deepseek").json("JSON", "test")
        self.assertIn("未返回可解析", str(raised.exception))
        self.assertNotIn("private-proxy-body", str(raised.exception))

    def test_network_permission_errors_fail_fast_without_secret_details(self):
        for provider in ("deepseek", "kimi"):
            for error in (PermissionError(1, "private-detail"), urllib.error.URLError(PermissionError(13, "private-detail"))):
                with self.subTest(provider=provider, error=type(error).__name__), \
                     patch.dict(os.environ, {OFFICIAL[provider][1]: "test-fake-key"}), \
                     patch("paper_pipeline.providers.urllib.request.build_opener") as build, \
                     patch("paper_pipeline.providers.time.sleep") as sleep:
                    build.return_value.open.side_effect = error
                    with self.assertRaisesRegex(RuntimeError, "EPERM/EACCES") as raised:
                        OfficialClient(self.config, provider).json("JSON", "test")
                    self.assertNotIn("private-detail", str(raised.exception))
                    build.return_value.open.assert_called_once()
                    sleep.assert_not_called()

    def test_env_reader_does_not_evaluate_shell_or_overwrite_environment(self):
        path = self.directory / ".env"
        path.write_text('DEEPSEEK_API_KEY="$(echo not-executed)"\nMOONSHOT_API_KEY=file-value\nPATH=forbidden\n')
        with patch.dict(os.environ, {"MOONSHOT_API_KEY": "existing", "PATH": "original"}, clear=True):
            load_env(path)
            self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "$(echo not-executed)")
            self.assertEqual(os.environ["MOONSHOT_API_KEY"], "existing")
            self.assertEqual(os.environ["PATH"], "original")

    def test_search_and_fetch_use_dedicated_deepseek_endpoints(self):
        search = DeepSeekSearch(self.config)
        with patch.object(search.client, "post", return_value={"results": [{"url": "https://arxiv.org/abs/2601.00001"}]}) as post:
            self.assertEqual(len(search.search("recommender", 5)), 1)
            self.assertEqual(post.call_args.args[0], "/v1/tools/search")
        with patch.object(search.client, "post", return_value={"content": "# Text", "title": "test"}) as post:
            search.fetch("https://arxiv.org/html/2601.00001")
            self.assertEqual(post.call_args.args[0], "/v1/tools/fetch")
        with self.assertRaises(ValueError):
            search.search("x" * 129)

    def test_html_retains_authors_tables_and_sections_without_scripts(self):
        raw = b'<article class="ltx_document"><h1>Title</h1><div class="ltx_authors">Test Author, Meta</div><h2>Results</h2><p>Text</p><table><tr><td>Recall</td><td>12%</td></tr></table><script>evil()</script></article>'
        blocks, metadata = extract_html(raw)
        text = " ".join(block["text"] for block in blocks)
        self.assertIn("Test Author, Meta", text)
        self.assertIn("Recall 12%", text)
        self.assertNotIn("evil", text)
        self.assertEqual(blocks[-1]["section"], "Results")
        self.assertEqual(metadata["format"], "html")

    def test_pdf_extraction_reads_every_page(self):
        import fitz
        pdf = fitz.open()
        for page_number in range(3):
            pdf.new_page().insert_text((72, 72), f"Synthetic text on page {page_number + 1}.")
        raw = pdf.tobytes()
        pdf.close()
        blocks, metadata = extract_pdf(raw)
        self.assertEqual({block["page"] for block in blocks}, {1, 2, 3})
        self.assertEqual(metadata["page_count"], 3)

    def test_invalid_nested_json_structure_still_fails(self):
        for mutation in ("object", "missing", "array", "item", "reference", "boolean", "quote", "rating"):
            result = analysis()
            if mutation == "object": result = []
            if mutation == "missing": del result["method"]
            if mutation == "array": result["key_results"] = {}
            if mutation == "item": result["affiliations"] = ["not-an-object"]
            if mutation == "reference": result["insights"][0]["evidence_ids"] = [123]
            if mutation == "boolean": result["insights"][0]["is_inference"] = "true"
            if mutation == "quote": result["evidence"][0]["quote"] = None
            if mutation == "rating": result["rating"] = True
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate_analysis(result)

    def test_json_structure_failures_retry_once_and_remain_errors(self):
        client = FakeClient(self.config)
        paper = {"title": "Test", "arxiv_id": "2601.00001"}
        invalid = analysis()
        invalid["key_results"] = "invalid array"
        with patch.object(client, "json", side_effect=[(invalid, {}), (analysis(), {})]) as call, \
             self.assertLogs("paper_pipeline.insights", level="WARNING"):
            result = analyze(paper, document(), client, self.config, self.directory)
        self.assertEqual(result["analysis"], analysis())
        self.assertEqual(call.call_count, 2)
        with patch.object(client, "json", return_value=(invalid, {})) as call, \
             self.assertLogs("paper_pipeline.insights", level="WARNING"), \
             self.assertRaisesRegex(ValueError, "JSON"):
            analyze(paper, document(), client, self.config, self.directory / "other")
        self.assertEqual(call.call_count, 2)

    def test_analysis_cache_and_budget_do_not_silently_truncate(self):
        client = FakeClient(self.config)
        paper = {"title": "Test", "arxiv_id": "2601.00001"}
        first = analyze(paper, document(), client, self.config, self.directory)
        second = analyze(paper, document(), client, self.config, self.directory)
        self.assertEqual(first, second)
        self.assertEqual(len(client.calls), 1)
        oversized = {**document(), "content_chars": self.config["digest"]["max_fulltext_chars"] + 1}
        with self.assertRaisesRegex(ValueError, "未截断"):
            analyze(paper, oversized, client, self.config, self.directory)
        self.assertEqual(len(client.calls), 1)

    def test_synthesis_is_cached(self):
        client = FakeClient(self.config)
        items = [{"paper": {"arxiv_id": "known"}, "insight": {"analysis": analysis()}}]
        result = {"trends": [], "topic": {"title": "Topic", "analysis": "Inference", "paper_ids": ["known"]}}
        with patch.object(client, "json", return_value=(result, {})) as call:
            first = synthesize(items, client, self.directory)
            cached = synthesize(items, client, self.directory)
        self.assertEqual(first, cached)
        call.assert_called_once()

    @patch("paper_pipeline.daily.FullTextStore", FakeFullText)
    @patch("paper_pipeline.daily.OfficialClient", FakeClient)
    def test_daily_publishes_with_broken_links_escaped(self):
        connection, _, args = self.seed()
        original_json = FakeClient.json

        def mismatched_json(client, system, user):
            result, metadata = original_json(client, system, user)
            if user.startswith("["):
                result["topic"]["paper_ids"] = ["unknown-paper<script>"]
                result["trends"] = [copy.deepcopy(result["topic"])]
            else:
                result["key_results"][0]["finding"] = "Recall 提升 98%。"
                result["evidence"][0]["quote"] = "not in source"
                result["insights"][0]["evidence_ids"] = ["E999"]
            return result, metadata

        try:
            with patch.object(FakeClient, "json", mismatched_json):
                result = digest(args, self.config, connection)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["errors"], [])
            self.assertEqual(result["fulltext_insights"], 2)
            self.assertEqual(result["pending"], 1)
            self.assertEqual(len(FakeClient.calls), 3)  # Two papers plus synthesis.
            payload = json.loads(Path(result["json"]).read_text())
            markdown, webpage = render_daily(payload)
            self.assertNotIn("unknown-paper<script>", webpage)
            self.assertNotIn("unknown-paper<script>", markdown)
            self.assertIn("unknown-paper&lt;script&gt;", webpage)
            self.assertNotIn('href="#paper-unknown', webpage)
            self.assertNotIn('-E999"', webpage)
            self.assertIn("[E999]", webpage)
            self.assertNotIn("已程序校验", payload["note"])
        finally:
            connection.close()

    @patch("paper_pipeline.daily.FullTextStore", FakeFullText)
    @patch("paper_pipeline.daily.OfficialClient", FakeClient)
    def test_complete_daily_writes_four_sections_archive_and_clears_only_completed_jobs(self):
        connection, _, args = self.seed()
        try:
            result = digest(args, self.config, connection)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["fulltext_insights"], 2)
            self.assertEqual(result["pending"], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM digest_processed").fetchone()[0], 2)
            text = Path(result["markdown"]).read_text()
            for section in ("当日样本观察", "今日速览", "全文 insight", "今日专题", "证据定位"):
                self.assertIn(section, text)
            self.assertIn("2026-10-23.html", Path(result["archive"]).read_text())
            self.assertIn("<style>", Path(result["html"]).read_text())
            self.assertNotIn('rel="stylesheet"', Path(result["html"]).read_text())
        finally:
            connection.close()

    @patch("paper_pipeline.daily.FullTextStore", FakeFullText)
    @patch("paper_pipeline.daily.OfficialClient", FakeClient)
    def test_fulltext_identified_companies_are_persisted_to_papers(self):
        connection, papers, args = self.seed()
        target = papers[1]["arxiv_id"]  # No arXiv affiliation; Kuaishou comes from full-text affiliations.
        try:
            before = json.loads(connection.execute("SELECT metadata_json FROM papers WHERE arxiv_id=?",
                                                   (target,)).fetchone()[0])
            self.assertEqual(before["companies"], [])
            digest(args, self.config, connection)
            after = json.loads(connection.execute("SELECT metadata_json FROM papers WHERE arxiv_id=?",
                                                  (target,)).fetchone()[0])
            self.assertEqual(after["companies"], ["Kuaishou"])
            self.assertEqual(after["title"], before["title"])
        finally:
            connection.close()

    @patch("paper_pipeline.daily.FullTextStore", FakeFullText)
    @patch("paper_pipeline.daily.OfficialClient", FakeClient)
    def test_daily_accepts_earlier_papers_but_excludes_the_day_after_cutoff(self):
        connection, papers, args = self.seed()
        next_day = {**papers[0], "arxiv_id": "2610.12345", "updated": "2026-10-23T00:00:00+00:00"}
        unchosen_same_day = {**papers[0], "arxiv_id": "2610.12346"}
        with connection:
            save_paper(connection, 1, next_day, self.config, selection_override=selection(next_day, self.config))
            save_paper(connection, 1, unchosen_same_day, self.config,
                       selection_override=selection(unchosen_same_day, self.config))
            connection.execute("DELETE FROM digest_queue WHERE arxiv_id<>?", (next_day["arxiv_id"],))
        connection.close()
        config_path = self.directory / "config.json"
        config_path.write_text(json.dumps(self.config))
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("paper_pipeline.providers.OfficialClient.require_key", return_value="test-fake-key"), \
             patch("paper_pipeline.discovery.SearchAgent.run", return_value={"summary": "Synthetic test", "papers": [
                 {"arxiv_id": paper["arxiv_id"], "source_url": paper["url"], "bucket": "recsys",
                  "priority": 5, "reason": "Synthetic test"} for paper in papers[:2]]}) as collect, \
             patch("paper_pipeline.discovery.ArxivClient.request", return_value=__import__("test_pipeline").feed([0, 1])), \
             patch("paper_pipeline.discovery.utcnow", return_value=timestamp("2026-10-24T00:00:00Z")), \
             patch("paper_pipeline.cli.LOG_ROOT", self.directory / "logs"), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            status = main(["--config", str(config_path), "--db", str(args.db), "daily", "--provider", "deepseek",
                           "--since", "2026-10-22", "--until", "2026-10-22"])
        self.assertEqual(status, 0, stderr.getvalue())
        # 日期窗口按 UTC+8 解释：2026-10-22 这一天是 2026-10-21T16:00Z 到 2026-10-22T16:00Z。
        self.assertEqual(collect.call_args.args[:2], (timestamp("2026-10-15T16:00:00Z"), timestamp("2026-10-22T16:00:00Z")))
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["date"], "2026-10-22")
        self.assertEqual(result["fulltext_insights"], 2)
        self.assertTrue(result["webpage_ready"])
        self.assertIn("日报网页已生成", stderr.getvalue())
        self.assertIn(str(Path(result["html"]).resolve()), stderr.getvalue())
        self.assertEqual(result["pending_in_window"], 0)
        self.assertEqual(result["pending"], 1)
        payload = json.loads(Path(result["json"]).read_text())
        self.assertEqual({i["paper"]["arxiv_id"] for i in payload["items"]}, {p["arxiv_id"] for p in papers[:2]})
        self.assertEqual(payload["paper_window"]["until_utc_exclusive"], "2026-10-22T16:00:00+00:00")
        connection = connect(args.db)
        try:
            self.assertTrue(connection.execute("SELECT 1 FROM digest_queue WHERE arxiv_id=?", (next_day["arxiv_id"],)).fetchone())
            self.assertFalse(connection.execute("SELECT 1 FROM digest_queue WHERE arxiv_id=?", (unchosen_same_day["arxiv_id"],)).fetchone())
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM state").fetchone()[0], 0)
        finally:
            connection.close()

    def test_one_command_finishes_agent_fulltext_summary_and_standalone_webpage(self):
        """Exercise the whole daily path; only remote IO and PDF text are synthetic."""
        from test_pipeline import feed
        self.config["discovery"]["max_rounds"] = 3
        config_path = self.directory / "config.json"
        config_path.write_text(json.dumps(self.config))
        url = "https://arxiv.org/abs/2601.00001"
        phases, tool_round = [], 0

        def response(client, path, payload, **kwargs):
            nonlocal tool_round
            if path == "/v1/tools/search":
                return {"results": [{"url": url, "summary": "Synthetic recommender paper"}]}
            if path == "/v1/tools/fetch":
                return {"title": "Synthetic paper", "content": "Synthetic author and abstract " + url}
            if "tools" in payload:
                tool_round += 1
                name, args = ("web_fetch", {"url": url}) if tool_round == 2 else ("web_search", {"query": "recsys " + str(tool_round)})
                return {"choices": [{"finish_reason": "tool_calls", "message": {
                    "role": "assistant", "content": None, "reasoning_content": "Synthetic reasoning",
                    "tool_calls": [{"id": "tool-" + str(tool_round), "type": "function",
                                    "function": {"name": name, "arguments": json.dumps(args)}}]}}]}
            system, user = payload["messages"][0]["content"], payload["messages"][1]["content"]
            if system.startswith("你负责整理推荐系统论文检索结果"):
                phases.append("finalization")
                result = {"summary": "Synthetic selection", "papers": [{"arxiv_id": "2601.00001", "source_url": url,
                    "bucket": "recsys", "priority": 5, "reason": "Synthetic recommendation method"}]}
            elif user.startswith("["):
                phases.append("synthesis")
                result = {"trends": [], "topic": {"title": "合成测试专题", "analysis": "合成测试综合总结",
                                                   "paper_ids": ["2601.00001"]}}
            else:
                phases.append("fulltext")
                result = analysis()
            return {"model": client.model, "usage": {"total_tokens": 100},
                    "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}]}

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("paper_pipeline.providers.OfficialClient.require_key", return_value="synthetic-key"), \
             patch("paper_pipeline.providers.OfficialClient.post", autospec=True, side_effect=response), \
             patch("paper_pipeline.discovery.ArxivClient.request", return_value=feed([0])), \
             patch("paper_pipeline.daily.FullTextStore", FakeFullText), \
             patch("paper_pipeline.cli.LOG_ROOT", self.directory / "logs"), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            status = main(["--config", str(config_path), "--db", str(self.directory / "papers.sqlite3"),
                           "daily", "--provider", "deepseek", "--since", "2026-10-22", "--until", "2026-10-22", "--limit", "10"])
        self.assertEqual(status, 0, stderr.getvalue())
        self.assertEqual(phases, ["finalization", "fulltext", "synthesis"])
        result = json.loads(stdout.getvalue())
        self.assertTrue(result["webpage_ready"])
        webpage = Path(result["html"]).read_text()
        for text in ("当日样本观察", "今日速览", "重点论文", "今日专题", "合成测试综合总结", "<style>"):
            self.assertIn(text, webpage)
        self.assertNotIn('rel="stylesheet"', webpage)
        self.assertNotIn('<script src=', webpage)
        self.assertIn("日报网页已生成", stderr.getvalue())
        self.assertIn("2026-10-22.html", Path(result["archive"]).read_text())

    def test_empty_daily_does_not_report_a_completed_summary(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("paper_pipeline.providers.OfficialClient.require_key", return_value="synthetic-key"), \
             patch("paper_pipeline.discovery.SearchAgent.run", return_value={"summary": "No matching papers", "papers": []}), \
             patch("paper_pipeline.cli.LOG_ROOT", self.directory / "logs"), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            status = main(["--db", str(self.directory / "papers.sqlite3"), "daily", "--provider", "deepseek",
                           "--since", "2026-10-22", "--until", "2026-10-22"])
        self.assertEqual(status, 1)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["status"], "empty")
        self.assertFalse(result["webpage_ready"])
        self.assertNotIn("日报网页已生成", stderr.getvalue())
        self.assertIn("尚未生成论文总结", Path(result["html"]).read_text())

    def test_day_window_is_inclusive_for_end_date_and_exclusive_for_next_midnight(self):
        # 日期串按 UTC+8 解释：2026-10-22 这一天对应 2026-10-21T16:00Z（含）到 2026-10-22T16:00Z（不含）。
        since = date_boundary("2026-10-22")
        until = date_boundary("2026-10-22", end=True)
        self.assertEqual((since, until), (timestamp("2026-10-21T16:00:00Z"), timestamp("2026-10-22T16:00:00Z")))
        for updated, expected in (("2026-10-21T15:59:59Z", False), ("2026-10-21T16:00:00Z", True),
                                  ("2026-10-22T15:59:59Z", True), ("2026-10-22T16:00:00Z", False)):
            with self.subTest(updated=updated):
                self.assertEqual(in_window({"updated": updated}, since, until), expected)
        self.assertFalse(in_window({"updated": None}, since, until))
        self.assertTrue(in_window({"updated": None}, None, None))

    @patch("paper_pipeline.daily.FullTextStore", FakeFullText)
    @patch("paper_pipeline.daily.OfficialClient", FakeClient)
    def test_date_window_filters_prior_edition_and_is_inherited_on_retry(self):
        connection, papers, args = self.seed()
        args.date = "2026-10-22"
        try:
            first = digest(args, self.config, connection)
            self.assertEqual(first["fulltext_insights"], 2)
            args.since = args.until = "2026-10-22"
            filtered = digest(args, self.config, connection)
            self.assertEqual(filtered["fulltext_insights"], 1)
            payload = json.loads(Path(filtered["json"]).read_text())
            self.assertEqual(payload["items"][0]["paper"]["arxiv_id"], papers[0]["arxiv_id"])
            calls = len(FakeClient.calls)
            args.since = args.until = None
            retried = digest(args, self.config, connection)
            self.assertEqual(retried["fulltext_insights"], 1)
            self.assertEqual(retried["paper_window"], filtered["paper_window"])
            self.assertEqual(len(FakeClient.calls), calls)
            self.assertEqual(retried["pending_in_window"], 0)
        finally:
            connection.close()

    @patch("paper_pipeline.daily.FullTextStore", FakeFullText)
    @patch("paper_pipeline.daily.OfficialClient", FakeClient)
    def test_synthesis_failure_keeps_jobs_and_retry_reuses_fulltext_insights(self):
        connection, _, args = self.seed()
        try:
            FakeClient.fail_synthesis = True
            failed = digest(args, self.config, connection)
            self.assertEqual(failed["status"], "partial")
            self.assertFalse(failed["webpage_ready"])
            self.assertEqual(failed["pending"], 3)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM digest_processed").fetchone()[0], 0)
            prior_calls = len(FakeClient.calls)
            FakeClient.fail_synthesis = False
            recovered = digest(args, self.config, connection)
            self.assertEqual(recovered["status"], "completed")
            self.assertEqual(recovered["pending"], 1)
            self.assertEqual(len(FakeClient.calls) - prior_calls, 1)  # Only synthesis is retried.
        finally:
            connection.close()

    @patch("paper_pipeline.daily.FullTextStore", FakeFullText)
    @patch("paper_pipeline.daily.OfficialClient", FakeClient)
    def test_publication_failure_rolls_back_processed_state(self):
        connection, _, args = self.seed()
        try:
            with patch("paper_pipeline.publishing.publish_daily", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    digest(args, self.config, connection)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM digest_queue").fetchone()[0], 3)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM digest_processed").fetchone()[0], 0)
        finally:
            connection.close()

    @patch("paper_pipeline.daily.FullTextStore", FakeFullText)
    @patch("paper_pipeline.daily.OfficialClient", FakeClient)
    def test_html_escapes_model_output(self):
        connection, _, args = self.seed()
        try:
            result = digest(args, self.config, connection)
            payload = json.loads(Path(result["json"]).read_text())
            payload["items"][0]["insight"]["analysis"]["title_zh"] = '<img src=x onerror="alert(1)">'
            _, webpage = render_daily(payload)
            self.assertNotIn('<img src=x', webpage)
            self.assertIn('&lt;img', webpage)
        finally:
            connection.close()

    def test_company_affiliations_exclude_schools_and_untrusted_company_claims(self):
        paper = {"affiliations": [], "companies": ["Alibaba"], "company_evidence_kind": "history_report"}
        result = organization_details(paper, {"affiliations": [
            {"organization": "University of Sydney"}, {"organization": "Alibaba Group"},
            {"organization": "Nankai University"}, {"organization": "Norly Research"}]}, self.config)
        self.assertEqual(result["organizations"], ["Alibaba"])
        self.assertIn("模型提取", result["organization_basis"])
        self.assertEqual(organization_details(paper, {}, self.config)["organizations"], [])
        paper["abstract"] = "We use Amazon datasets and Meta models."
        self.assertEqual(organization_details(paper, {}, self.config)["organizations"], [])
        paper["affiliations"] = ["Metadata University", "Kuaishou Technology"]
        self.assertEqual(organization_details(paper, {}, self.config)["organizations"], ["Kuaishou"])
        paper["affiliations"] = []
        paper["company_evidence_kind"] = "arxiv_affiliation"
        self.assertEqual(organization_details(paper, {}, self.config)["organizations"], ["Alibaba"])

    @patch("paper_pipeline.daily.FullTextStore", FakeFullText)
    @patch("paper_pipeline.daily.OfficialClient", FakeClient)
    def test_company_tags_and_search_exclude_university_collaborators(self):
        connection, _, args = self.seed()
        try:
            result = digest(args, self.config, connection)
            payload = json.loads(Path(result["json"]).read_text())
            for item in payload["items"]:
                item["insight"]["analysis"]["affiliations"] = [
                    {"organization": "University of Sydney", "evidence_id": "E2"},
                    {"organization": "Unisound AI", "evidence_id": "E2"},
                    {"organization": '<img src=x onerror="alert(1)">', "evidence_id": "E2"}]
                item["companies"] = ["Alibaba"]
                item["preferred_companies"] = ["Alibaba"]
            markdown, webpage = render_daily(payload)
            for name in ("University of Sydney", "Unisound AI"):
                self.assertNotIn(name, markdown)
                self.assertNotIn(name, webpage)
            self.assertNotIn("机构待核验", webpage)
            self.assertNotIn('<img src=x', webpage)
            from bs4 import BeautifulSoup
            cards = BeautifulSoup(webpage, "html.parser").select("article.paper")
            self.assertTrue(all("University of Sydney" not in card["data-search"] for card in cards))
            self.assertTrue(all("Alibaba" in card.select_one(".paper-meta").get_text() for card in cards))
        finally:
            connection.close()

    @patch("paper_pipeline.daily.FullTextStore", FakeFullText)
    @patch("paper_pipeline.daily.OfficialClient", FakeClient)
    def test_rebuild_keeps_only_company_tags_without_model_calls_or_queue_changes(self):
        connection, _, args = self.seed()
        try:
            result = digest(args, self.config, connection)
            payload = json.loads(Path(result["json"]).read_text())
            item = payload["items"][0]
            item["insight"]["analysis"]["affiliations"] = [
                {"organization": "Nankai University", "evidence_id": "E2"},
                {"organization": "Alibaba Group", "evidence_id": "E2"}]
            item["companies"], item["preferred_companies"] = [], []
            item.pop("organizations")
            item.pop("organization_basis")
            with connection:
                connection.execute("UPDATE editions SET payload_json=? WHERE day=?", (json.dumps(payload), payload["date"]))
            before_queue = [tuple(row) for row in connection.execute("SELECT * FROM digest_queue")]
            before_processed = [tuple(row) for row in connection.execute("SELECT * FROM digest_processed")]
            calls = len(FakeClient.calls)
            with patch("paper_pipeline.providers.OfficialClient.post", side_effect=AssertionError("No API calls during rebuild")), \
                 patch("paper_pipeline.arxiv.ArxivClient.request", side_effect=AssertionError("No network during rebuild")):
                result = rebuild_reports(connection, args.output_dir, self.config)
            self.assertEqual(result["editions_rebuilt"], 1)
            updated = json.loads(connection.execute("SELECT payload_json FROM editions").fetchone()[0])
            self.assertEqual(updated["items"][0]["organizations"], ["Alibaba"])
            self.assertEqual(updated["items"][0]["insight"], item["insight"])
            self.assertEqual(updated["items"][0]["companies"], ["Alibaba"])
            self.assertEqual(updated["generated_at"], payload["generated_at"])
            self.assertEqual(updated["synthesis"], payload["synthesis"])
            self.assertEqual(len(FakeClient.calls), calls)
            self.assertEqual(before_queue, [tuple(row) for row in connection.execute("SELECT * FROM digest_queue")])
            self.assertEqual(before_processed, [tuple(row) for row in connection.execute("SELECT * FROM digest_processed")])
            self.assertNotIn("Nankai University", Path(result["archive"]).read_text())
            self.assertNotIn("Nankai University", (args.output_dir / "site" / (payload["date"] + ".html")).read_text())
            self.assertIn("Alibaba", Path(result["archive"]).read_text())
        finally:
            connection.close()

    @patch("paper_pipeline.daily.FullTextStore", FakeFullText)
    @patch("paper_pipeline.daily.OfficialClient", FakeClient)
    def test_school_paper_does_not_use_limit_and_is_excluded_from_synthesis(self):
        connection, _, args = self.seed()
        args.limit = 1
        jobs = queued(connection)
        rejected_id, accepted_id = [job["paper"]["arxiv_id"] for job in jobs[:2]]
        paper = jobs[0]["paper"]
        paper.update(affiliations=[], companies=[], company_evidence_kind="unknown")
        with connection:
            connection.execute("UPDATE digest_queue SET metadata_json=? WHERE arxiv_id=?", (json.dumps(paper), rejected_id))

        def fake_analyze(paper, *args):
            result = analysis()
            if paper["arxiv_id"] == rejected_id:
                result["affiliations"] = [{"organization": "Anhui University", "evidence_id": "E2"}]
            return {"analysis": result, "extraction": {"note": "Synthetic test"}}

        try:
            with patch("paper_pipeline.daily.analyze", side_effect=fake_analyze) as call:
                result = digest(args, self.config, connection)
            payload = json.loads(Path(result["json"]).read_text())
            self.assertEqual(call.call_count, 2)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["fulltext_insights"], 1)
            self.assertEqual(result["pending"], 1)
            self.assertEqual(payload["items"][0]["paper"]["arxiv_id"], accepted_id)
            self.assertEqual(payload["rejected"][0]["arxiv_id"], rejected_id)
            self.assertIn("未确认目标大公司", payload["rejected"][0]["reason"])
            self.assertEqual(payload["synthesis"]["topic"]["paper_ids"], [accepted_id])
            self.assertEqual(connection.execute("SELECT outcome FROM digest_processed WHERE arxiv_id=?", (rejected_id,)).fetchone()[0], "out_of_scope")
        finally:
            connection.close()

    @patch("paper_pipeline.daily.FullTextStore", FakeFullText)
    @patch("paper_pipeline.daily.OfficialClient", FakeClient)
    def test_no_confirmed_company_yields_empty_report_without_synthesis(self):
        connection, _, args = self.seed()
        with connection:
            for job in queued(connection):
                paper = job["paper"]
                paper.update(affiliations=[], companies=["Meta"], company_evidence_kind="history_report")
                connection.execute("UPDATE digest_queue SET metadata_json=? WHERE arxiv_id=?", (json.dumps(paper), paper["arxiv_id"]))
        result_analysis = analysis()
        result_analysis["affiliations"] = []
        try:
            with patch("paper_pipeline.daily.analyze", return_value={"analysis": result_analysis}):
                result = digest(args, self.config, connection)
            self.assertEqual(result["status"], "empty")
            self.assertFalse(result["webpage_ready"])
            self.assertEqual(result["pending"], 0)
            self.assertEqual(len(FakeClient.calls), 0)
            self.assertEqual(len(json.loads(Path(result["json"]).read_text())["rejected"]), 3)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
