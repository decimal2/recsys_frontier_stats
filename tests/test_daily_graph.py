"""Daily graph recovery at real paper and publication boundaries, offline only."""
import io
import json
import os
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from test_insights import FakeClient, FakeFullText
from test_pipeline import ROOT, selection
from paper_pipeline.arxiv import parse_atom
from paper_pipeline.cli import load_config, main
from paper_pipeline.daily import digest
from paper_pipeline.insights import analyze
from paper_pipeline.runtime import get_task, list_tasks
from paper_pipeline.storage import connect, save_paper, start_run


class DailyGraphTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.config = load_config(ROOT / "pipeline_config.json")
        self.config["digest"]["min_papers"] = 1
        self.args = Namespace(command="digest", provider="deepseek", date="2026-10-23",
                              since=None, until=None, run_id=None, limit=2,
                              db=self.directory / "papers.sqlite3", output_dir=self.directory / "reports",
                              config=self.directory / "config.json")
        self.args.config.write_text(json.dumps(self.config))
        FakeClient.calls, FakeClient.fail_synthesis = [], False
        self.connection = connect(self.args.db)
        self.addCleanup(lambda: self.connection.close())
        papers, _, _ = parse_atom((ROOT / "tests/fixtures/arxiv_sample.xml").read_bytes(), self.config)
        run_id = start_run(self.connection, "atom_import", self.config)
        with self.connection:
            for index, paper in enumerate(papers):
                save_paper(self.connection, run_id, paper, self.config,
                           selection_override=selection(paper, self.config, selected=index != 2))
        for target, replacement in (("paper_pipeline.daily.OfficialClient", FakeClient),
                                    ("paper_pipeline.daily.FullTextStore", FakeFullText)):
            stub = patch(target, replacement)
            stub.start()
            self.addCleanup(stub.stop)

    def reopen(self):
        self.connection.close()
        self.connection = connect(self.args.db)

    def test_interruption_after_first_paper_resumes_without_reanalysis(self):
        self.args.task_id = "daily-interruption"
        completed = []

        def interrupted_analysis(paper, *args):
            if completed:
                raise KeyboardInterrupt("synthetic process interruption")
            completed.append(paper["arxiv_id"])
            return analyze(paper, *args)

        with patch("paper_pipeline.daily.analyze", side_effect=interrupted_analysis):
            with self.assertRaises(KeyboardInterrupt):
                digest(self.args, self.config, self.connection)
        self.assertEqual(len(completed), 1)
        self.assertEqual(get_task(self.directory, self.args.task_id)["status"], "failed")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM editions").fetchone()[0], 0)
        self.reopen()
        self.args.resume = True
        with patch("paper_pipeline.daily.analyze", wraps=analyze) as reader:
            result = digest(self.args, self.config, self.connection)
        self.assertTrue(result["webpage_ready"])
        self.assertEqual(result["fulltext_insights"], 2)
        self.assertEqual(reader.call_count, 1)
        self.assertNotEqual(reader.call_args.args[0]["arxiv_id"], completed[0])
        payload = json.loads(Path(result["json"]).read_text())
        self.assertIn(completed[0], [item["paper"]["arxiv_id"] for item in payload["items"]])
        self.assertEqual(get_task(self.directory, self.args.task_id)["status"], "completed")
        self.assertEqual(len(FakeClient.calls), 3)  # Two paper insights and one synthesis.

    def test_publication_retry_reuses_checkpointed_synthesis_and_keeps_queue_atomic(self):
        self.args.task_id = "publication-retry"
        with patch("paper_pipeline.publishing.publish_daily", side_effect=OSError("synthetic disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                digest(self.args, self.config, self.connection)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM digest_queue").fetchone()[0], 3)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM digest_processed").fetchone()[0], 0)
        self.assertEqual(get_task(self.directory, self.args.task_id)["last_node"], "publish")
        self.reopen()
        self.args.resume = True
        with patch("paper_pipeline.daily.analyze", side_effect=AssertionError("analysis repeated")), \
             patch("paper_pipeline.daily.synthesize", side_effect=AssertionError("synthesis repeated")):
            result = digest(self.args, self.config, self.connection)
        self.assertTrue(result["webpage_ready"])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM digest_queue").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM digest_processed").fetchone()[0], 2)
        self.assertTrue(Path(result["html"]).is_file())

    def test_cli_resume_uses_saved_config_and_absolute_paths_from_another_directory(self):
        self.connection.close()
        old_cwd = Path.cwd()
        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            os.chdir(self.directory)
            with patch("paper_pipeline.cli.LOG_ROOT", self.directory / "logs"), \
                 patch("paper_pipeline.providers.OfficialClient.require_key", return_value="synthetic-key"), \
                 patch("paper_pipeline.publishing.publish_daily", side_effect=OSError("synthetic disk full")), \
                 redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(["--db", "papers.sqlite3", "--config", "config.json", "--output-dir", "custom-reports",
                             "digest", "--date", "2026-10-23", "--limit", "2"])
            self.assertEqual(code, 1)
            task = next(task for task in list_tasks(self.directory) if task["kind"] == "digest")
            # Resume must use the original configuration snapshot even after its file is gone.
            self.args.config.unlink()
            elsewhere = self.directory / "another-cwd"
            elsewhere.mkdir()
            os.chdir(elsewhere)
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch("paper_pipeline.cli.LOG_ROOT", self.directory / "logs"), \
                 patch("paper_pipeline.providers.OfficialClient.require_key", return_value="synthetic-key"), \
                 patch("paper_pipeline.daily.analyze", side_effect=AssertionError("analysis repeated")), \
                 patch("paper_pipeline.daily.synthesize", side_effect=AssertionError("synthesis repeated")), \
                 redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(["--db", str(self.args.db), "resume", task["task_id"]])
            self.assertEqual(code, 0, stderr.getvalue())
            result = json.loads(stdout.getvalue())
            self.assertEqual(result["task_id"], task["task_id"])
            self.assertTrue(result["webpage_ready"])
            self.assertEqual(Path(result["html"]).parent, self.directory / "custom-reports" / "site")
            self.assertFalse((elsewhere / "papers.sqlite3").exists())
        finally:
            os.chdir(old_cwd)

    def test_resumed_edition_excludes_papers_published_by_an_intervening_task(self):
        self.config["digest"]["min_papers"] = 2
        self.args.task_id = "earlier-edition"
        seen = []

        def interruption(paper, *args):
            if seen:
                raise KeyboardInterrupt("synthetic interruption before second paper")
            seen.append(paper["arxiv_id"])
            return analyze(paper, *args)

        with patch("paper_pipeline.daily.analyze", side_effect=interruption):
            with self.assertRaises(KeyboardInterrupt):
                digest(self.args, self.config, self.connection)
        later = Namespace(**{**vars(self.args), "task_id": "later-edition", "date": "2026-10-24"})
        new_result = digest(later, self.config, self.connection)
        new_payload = json.loads(Path(new_result["json"]).read_text())
        new_ids = {item["paper"]["arxiv_id"] for item in new_payload["items"]}
        self.assertIn(seen[0], new_ids)
        self.args.resume = True
        recovered = digest(self.args, self.config, self.connection)
        recovered_payload = json.loads(Path(recovered["json"]).read_text())
        recovered_ids = {item["paper"]["arxiv_id"] for item in recovered_payload["items"]}
        self.assertFalse(new_ids & recovered_ids)
        self.assertEqual(recovered["status"], "insufficient")
        self.assertEqual(recovered["coverage"], {"minimum": 2, "target": 2, "actual": 1, "shortfall": 1})
        self.assertEqual(json.loads(Path(new_result["json"]).read_text()), new_payload)

    def test_resumed_task_cannot_overwrite_newer_report_for_the_same_day(self):
        self.args.task_id = "older-same-day"
        seen = []

        def interruption(paper, *args):
            if seen:
                raise KeyboardInterrupt("synthetic interruption before second paper")
            seen.append(paper["arxiv_id"])
            return analyze(paper, *args)

        with patch("paper_pipeline.daily.analyze", side_effect=interruption):
            with self.assertRaises(KeyboardInterrupt):
                digest(self.args, self.config, self.connection)
        newer_args = Namespace(**{**vars(self.args), "task_id": "newer-same-day", "limit": 3})
        newer = digest(newer_args, self.config, self.connection)
        self.assertEqual(newer["fulltext_insights"], 3)
        snapshot = {key: Path(newer[key]).read_bytes() for key in ("json", "html", "markdown")}
        saved_payload = self.connection.execute("SELECT payload_json FROM editions WHERE day=?", (self.args.date,)).fetchone()[0]
        self.args.resume = True
        with self.assertRaisesRegex(ValueError, r"日报.*(更新|变更|改变)|edition.*chang"):
            digest(self.args, self.config, self.connection)
        for key, content in snapshot.items():
            self.assertEqual(Path(newer[key]).read_bytes(), content)
        self.assertEqual(self.connection.execute("SELECT payload_json FROM editions WHERE day=?", (self.args.date,)).fetchone()[0], saved_payload)

    def test_finished_insufficient_task_remains_insufficient_but_new_run_can_fill_it(self):
        self.config["digest"]["min_papers"] = 4
        self.args.limit = 4
        first = digest(self.args, self.config, self.connection)
        self.assertEqual(first["status"], "insufficient")
        self.assertEqual(first["coverage"]["shortfall"], 1)
        self.args.task_id, self.args.resume = first["task_id"], True
        with patch("paper_pipeline.daily.analyze", side_effect=AssertionError("finished task repeated")):
            resumed = digest(self.args, self.config, self.connection)
        self.assertEqual(resumed, first)
        # Existing rerun semantics accept new queue work using a new graph task.
        row = self.connection.execute("SELECT metadata_json,selection_json FROM papers LIMIT 1").fetchone()
        paper, selection = json.loads(row[0]), json.loads(row[1])
        paper["arxiv_id"], paper["title"] = "2610.99998", "Synthetic new industrial paper"
        paper["url"], paper["pdf_url"] = "https://arxiv.org/abs/2610.99998", "https://arxiv.org/pdf/2610.99998"
        run_id = start_run(self.connection, "atom_import", self.config)
        with self.connection:
            save_paper(self.connection, run_id, paper, self.config, selection_override=selection)
        self.args.task_id, self.args.resume = None, False
        with patch("paper_pipeline.daily.analyze", wraps=analyze) as reader:
            completed = digest(self.args, self.config, self.connection)
        self.assertTrue(completed["webpage_ready"])
        self.assertEqual(completed["coverage"]["actual"], 4)
        reader.assert_called_once()
        self.assertNotEqual(completed["task_id"], first["task_id"])


if __name__ == "__main__":
    unittest.main()
