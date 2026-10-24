"""Persistent search graphs use synthetic model and webpage responses only."""
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from test_pipeline import ROOT, feed
from test_discovery import BLOG, URL, candidate, turn
from paper_pipeline.arxiv import timestamp
from paper_pipeline.cli import load_config
from paper_pipeline.discovery import SearchAgent, discover
from paper_pipeline.runtime import get_task
from paper_pipeline.storage import connect


class DiscoveryGraphTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.config = load_config(ROOT / "pipeline_config.json")
        self.since = timestamp("2026-10-22T00:00:00Z")
        self.until = timestamp("2026-10-23T00:00:00Z")

    def test_restart_reuses_each_completed_tool_and_does_not_store_reasoning(self):
        agent = SearchAgent(self.config, "deepseek", self.directory)
        first_fetch, invocation = turn("web_fetch", {"url": BLOG}, "fetch-1")
        first_fetch["tool_calls"] += turn("web_fetch", {"url": URL}, "fetch-2")[0]["tool_calls"]
        turns = [turn("web_search", {"query": "company recommendation"}), (first_fetch, invocation)]
        with patch.object(agent.client, "tools", side_effect=turns), \
             patch.object(agent.web, "search", return_value=[{"url": BLOG}]), \
             patch.object(agent.web, "fetch", side_effect=[{"content": URL}, RuntimeError("interrupted fetch")]):
            with self.assertRaisesRegex(RuntimeError, "interrupted fetch"):
                agent.run(self.since, self.until, 10)
        self.assertEqual(get_task(self.directory, "search")["status"], "failed")

        restarted = SearchAgent(self.config, "deepseek", self.directory)
        selection = {"summary": "Recovered from saved evidence", "papers": [candidate(source=BLOG)]}
        with patch.object(restarted.client, "tools", return_value=turn("submit_papers", selection)) as completion, \
             patch.object(restarted.web, "search") as search, \
             patch.object(restarted.web, "fetch", return_value={"content": "Synthetic full text"}) as fetch:
            result = restarted.run(self.since, self.until, 10)
        self.assertEqual(result, selection)
        search.assert_not_called()
        fetch.assert_called_once_with(URL)
        context = json.loads(completion.call_args.args[0][1]["content"])
        self.assertIn(BLOG, context["observed_sources"]["2601.00001"])
        self.assertEqual(len(context["previous_tools"]), 3)
        self.assertEqual(get_task(self.directory, "search")["status"], "completed")
        for path in self.directory.iterdir():
            if path.is_file():
                self.assertNotIn(b"synthetic-private-reasoning", path.read_bytes())

    def test_metadata_resume_keeps_run_id_and_does_not_search_again(self):
        args = Namespace(provider="deepseek", since="2026-10-22", until="2026-10-22", limit=10,
                         db=self.directory / "papers.sqlite3", output_dir=self.directory / "reports",
                         task_id="metadata-retry", resume=False)
        connection = connect(args.db)
        self.addCleanup(connection.close)
        selection = {"summary": "Synthetic search", "papers": [candidate()]}
        with patch("paper_pipeline.providers.OfficialClient.require_key", return_value="synthetic-key"), \
             patch("paper_pipeline.discovery.SearchAgent.run", return_value=selection) as search, \
             patch("paper_pipeline.discovery.ArxivClient.request", side_effect=RuntimeError("metadata offline")):
            with self.assertRaisesRegex(RuntimeError, "metadata offline"):
                discover(args, self.config, connection)
        search.assert_called_once()
        saved = get_task(self.directory, "metadata-retry")
        self.assertEqual(saved["kind"], "discover")
        self.assertEqual(saved["status"], "failed")
        run_id = saved["request"]["run_id"]
        args.resume = True
        with patch("paper_pipeline.providers.OfficialClient.require_key", return_value="synthetic-key"), \
             patch("paper_pipeline.discovery.SearchAgent.run", side_effect=AssertionError("search repeated")), \
             patch("paper_pipeline.discovery.ArxivClient.request", return_value=feed([0])):
            output = discover(args, self.config, connection)
        self.assertEqual(output["run_id"], run_id)
        self.assertEqual(output["unique_seen"], 1)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 1)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 1)
        self.assertEqual(get_task(self.directory, "metadata-retry")["status"], "completed")

    def test_completed_daily_child_reuses_committed_output(self):
        args = Namespace(provider="deepseek", since="2026-10-22", until="2026-10-22", limit=10,
                         db=self.directory / "papers.sqlite3", output_dir=self.directory / "reports",
                         discovery_task_id="daily-123-search-0", resume=True)
        connection = connect(args.db)
        self.addCleanup(connection.close)
        selection = {"summary": "Synthetic search", "papers": [candidate()]}
        with patch("paper_pipeline.providers.OfficialClient.require_key", return_value="synthetic-key"), \
             patch("paper_pipeline.discovery.SearchAgent.run", return_value=selection) as search, \
             patch("paper_pipeline.discovery.ArxivClient.request", return_value=feed([0])) as metadata:
            first = discover(args, self.config, connection)
            second = discover(args, self.config, connection)
        self.assertEqual(first, second)
        search.assert_called_once()
        metadata.assert_called_once()
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
