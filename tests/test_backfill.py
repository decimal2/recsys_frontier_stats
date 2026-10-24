"""Daily quotas, rolling windows and deduplication with synthetic remote responses."""
import copy
import io
import json
import tempfile
import unittest
import urllib.parse
import xml.etree.ElementTree as ET
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from test_insights import FakeClient, FakeFullText, analysis
from test_pipeline import ROOT, PAYLOAD
from paper_pipeline.arxiv import NS, timestamp
from paper_pipeline.cli import load_config, main
from paper_pipeline.daily import digest
from paper_pipeline.storage import connect


class BackfillTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.config = load_config(ROOT / "pipeline_config.json")
        self.args = Namespace(command="daily", date="2026-10-22", since="2026-10-22",
                              until="2026-10-22", limit=10, run_id=None, provider="deepseek",
                              db=self.directory / "papers.sqlite3", output_dir=self.directory / "reports")
        self.connection = connect(self.args.db)
        self.addCleanup(self.connection.close)
        self.papers, self.batches, self.searches, self.metadata_requests, self.analyzed = {}, [], [], [], []
        FakeClient.calls, FakeClient.fail_synthesis = [], False
        patches = [
            patch("paper_pipeline.daily.OfficialClient", FakeClient),
            patch("paper_pipeline.daily.FullTextStore", FakeFullText),
            patch("paper_pipeline.daily.analyze", side_effect=self.analyze),
            patch("paper_pipeline.providers.OfficialClient.require_key", return_value="synthetic-key"),
            patch("paper_pipeline.discovery.SearchAgent.run", side_effect=self.search),
            patch("paper_pipeline.discovery.ArxivClient.request", side_effect=self.metadata),
            patch("paper_pipeline.daily.utcnow", return_value=timestamp("2026-10-24T12:00:00Z")),
            patch("paper_pipeline.discovery.utcnow", return_value=timestamp("2026-10-24T12:00:00Z")),
        ]
        for stub in patches:
            stub.start()
            self.addCleanup(stub.stop)

    def paper(self, index, day="2026-10-16", company=True):
        identifier = f"2610.{10000 + index:05d}"
        self.papers[identifier] = {"day": day, "company": company}
        return identifier

    def search(self, since, until, target, excluded_ids=None):
        self.searches.append({"since": since, "until": until, "target": target, "excluded": set(excluded_ids or [])})
        batch = self.batches.pop(0) if self.batches else []
        if isinstance(batch, Exception):
            raise batch
        return {"summary": "Synthetic search", "papers": [
            {"arxiv_id": identifier, "source_url": "https://arxiv.org/abs/" + identifier,
             "bucket": "recsys", "priority": 5, "reason": "Synthetic company paper"}
            for identifier in batch]}

    def metadata(self, url):
        identifiers = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["id_list"][0].split(",")
        self.metadata_requests.append(identifiers)
        root = ET.fromstring(PAYLOAD)
        template = copy.deepcopy(root.find("a:entry", NS))
        for entry in root.findall("a:entry", NS):
            root.remove(entry)
        for identifier in identifiers:
            paper = self.papers[identifier]
            entry = copy.deepcopy(template)
            entry.find("a:id", NS).text = "https://arxiv.org/abs/" + identifier + "v1"
            entry.find("a:title", NS).text = "Synthetic Industrial Recommendation " + identifier
            for field in ("published", "updated"):
                entry.find("a:" + field, NS).text = paper["day"] + "T10:00:00Z"
            for affiliation in entry.findall(".//ar:affiliation", NS):
                affiliation.text = "Kuaishou Technology" if paper["company"] else "Anhui University"
            root.append(entry)
        return ET.tostring(root)

    def analyze(self, paper, *args):
        identifier = paper["arxiv_id"]
        self.analyzed.append(identifier)
        result = analysis()
        if not self.papers[identifier]["company"]:
            result["affiliations"] = [{"organization": "Anhui University", "evidence_id": "E2"}]
        return {"analysis": result, "extraction": {"note": "Synthetic fixture"}}

    def test_backfill_counts_only_fulltext_confirmed_companies_and_stops_at_five(self):
        recent = [self.paper(i) for i in range(4)]
        school = self.paper(4, company=False)
        older = [self.paper(5, "2026-10-10")]
        future = self.paper(9, "2026-10-23")
        self.batches = [recent + [school], older + [recent[0], future]]
        result = digest(self.args, self.config, self.connection)
        self.assertTrue(result["webpage_ready"])
        self.assertEqual(result["coverage"], {"minimum": 5, "target": 10, "actual": 5, "shortfall": 0})
        self.assertEqual([s["since"].date().isoformat() for s in self.searches], ["2026-10-15", "2026-10-08"])
        self.assertTrue(all(s["until"] == timestamp("2026-10-22T16:00:00Z") for s in self.searches))  # UTC+8 日界
        self.assertTrue(set(recent + [school]) <= self.searches[1]["excluded"])
        self.assertNotIn(recent[0], self.metadata_requests[1])
        self.assertNotIn(future, self.analyzed)
        payload = json.loads(Path(result["json"]).read_text())
        self.assertEqual({i["paper"]["arxiv_id"] for i in payload["items"]}, set(recent + older))
        self.assertEqual(payload["rejected"][0]["arxiv_id"], school)
        self.assertEqual(len(FakeClient.calls), 1)  # Synthesis happens once, after all backfill rounds.
        self.assertEqual(set(payload["synthesis"]["topic"]["paper_ids"]), set(recent + older))
        retried = digest(self.args, self.config, self.connection)
        self.assertTrue(retried["webpage_ready"])
        self.assertEqual(len(self.searches), 2)
        self.assertEqual(len(self.analyzed), 6)
        self.assertEqual(len(FakeClient.calls), 1)

    def test_limit_caps_total_edition_and_does_not_grow_on_rerun(self):
        self.batches = [[self.paper(i) for i in range(12)]]
        result = digest(self.args, self.config, self.connection)
        self.assertEqual(result["fulltext_insights"], 10)
        self.assertEqual(result["pending"], 2)
        self.assertEqual(len(self.searches), 1)
        again = digest(self.args, self.config, self.connection)
        self.assertEqual(again["fulltext_insights"], 10)
        self.assertEqual(again["pending"], 2)
        self.assertEqual(len(self.analyzed), 10)

    def test_shortfall_is_visible_and_resume_keeps_old_insights_and_expanded_window(self):
        original = [self.paper(i) for i in range(3)]
        self.batches = [original, [], []]
        result = digest(self.args, self.config, self.connection)
        self.assertEqual(result["status"], "insufficient")
        self.assertFalse(result["webpage_ready"])
        self.assertEqual(result["coverage"]["shortfall"], 2)
        self.assertEqual([s["since"].date().isoformat() for s in self.searches],
                         ["2026-10-15", "2026-10-08", "2026-09-22"])
        self.assertIn("还差 2 篇", Path(result["html"]).read_text())
        self.batches = [[self.paper(3, "2026-09-28"), self.paper(4, "2026-09-28")]]
        again = digest(self.args, self.config, self.connection)
        self.assertTrue(again["webpage_ready"])
        self.assertEqual(again["fulltext_insights"], 5)
        self.assertEqual(len(self.analyzed), 5)
        self.assertEqual(len(self.searches), 4)
        self.assertEqual(self.searches[-1]["since"], timestamp("2026-09-22T16:00:00Z"))
        self.assertTrue(set(original) <= self.searches[-1]["excluded"])

    def test_later_edition_does_not_reuse_papers_even_if_agent_resubmits_them(self):
        first = [self.paper(i) for i in range(8)]
        self.batches = [first]
        digest(self.args, self.config, self.connection)
        second = [self.paper(i, "2026-10-22") for i in range(8, 16)]
        self.args.date = self.args.since = self.args.until = "2026-10-23"
        self.batches = [first + second]
        result = digest(self.args, self.config, self.connection)
        self.assertTrue(result["webpage_ready"])
        payload = json.loads(Path(result["json"]).read_text())
        self.assertEqual({i["paper"]["arxiv_id"] for i in payload["items"]}, set(second))
        self.assertTrue(set(first) <= self.searches[-1]["excluded"])
        self.assertFalse(set(first) & set(self.metadata_requests[-1]))

    def test_search_failure_keeps_completed_insights_and_reports_partial(self):
        self.batches = [[self.paper(i) for i in range(3)], RuntimeError("Synthetic API failure")]
        with self.assertLogs("paper_pipeline.daily", level="ERROR"):
            result = digest(self.args, self.config, self.connection)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["fulltext_insights"], 3)
        self.assertEqual(result["coverage"]["shortfall"], 2)
        self.assertEqual(result["errors"][0]["arxiv_id"], "discovery")
        self.assertTrue(Path(result["html"]).exists())

    def test_cli_shortfall_is_nonzero_and_small_limit_fails_before_network(self):
        self.batches = [[self.paper(i) for i in range(3)], [], []]
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("paper_pipeline.cli.LOG_ROOT", self.directory / "logs"), redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--db", str(self.args.db), "daily", "--date", "2026-10-22"])
        self.assertEqual(code, 1)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["date"], "2026-10-22")
        self.assertEqual(result["status"], "insufficient")
        self.assertNotIn("日报网页已生成", stderr.getvalue())
        self.assertIn("缺口 2 篇", stderr.getvalue())
        self.args.limit = 4
        with patch.object(FakeClient, "require_key") as key, self.assertRaisesRegex(ValueError, "min_papers"):
            digest(self.args, self.config, self.connection)
        key.assert_not_called()

    def test_invalid_minimum_or_expansion_windows_are_rejected(self):
        for key, value in (("min_papers", 0), ("min_papers", 11),
                           ("lookback_windows_days", []), ("lookback_windows_days", [14, 7]),
                           ("lookback_windows_days", [7, 7]), ("lookback_windows_days", [7, 400])):
            config = copy.deepcopy(self.config)
            config["digest"][key] = value
            path = self.directory / "config.json"
            path.write_text(json.dumps(config))
            with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, "digest"):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
