"""Publishing: Markdown/HTML render the same papers, companies are shown, index built once."""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_insights import analysis
from paper_pipeline.publishing import anchor, publish_index, rebuild_reports, render_daily
from paper_pipeline.storage import connect, dumps

CONFIG = {"companies": ["Kuaishou", "Meta", "Alibaba"], "preferred_companies": ["Kuaishou", "Meta"],
          "company_aliases": {}}


def paper(identifier="2601.00001", companies=()):
    return {"arxiv_id": identifier, "version": 2, "title": f"Synthetic Recommender Paper {identifier}",
            "url": "https://arxiv.org/abs/" + identifier, "pdf_url": "https://arxiv.org/pdf/" + identifier,
            "updated": "2026-10-22T10:00:00+00:00", "companies": sorted(companies)}


def payload(*papers, day="2026-10-23"):
    items = [{"paper": p, "selection": {"bucket": "recsys", "score": 110, "tags": []},
              "insight": {"analysis": analysis(), "extraction": {"note": "Synthetic fixture"}},
              "organizations": p["companies"], "organization_basis": "全文机构行（模型提取）",
              "companies": p["companies"], "company_basis": "全文机构行（模型提取）",
              "preferred_companies": sorted(set(p["companies"]) & set(CONFIG["preferred_companies"]))}
             for p in papers]
    return {"date": day, "generated_at": "2026-10-23T04:00:00+00:00", "status": "completed",
            "task_id": "publishing-test", "provider": "deepseek", "model": "test-model",
            "note": "基于提取到的全文文字生成。", "paper_window": None,
            "coverage": {"minimum": 1, "target": len(items), "actual": len(items), "shortfall": 0},
            "discovery": [], "items": items, "synthesis": {"trends": [], "topic": None},
            "errors": [], "rejected": []}


class PublishingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def test_markdown_and_html_render_the_same_paper_set(self):
        payload_data = payload(paper("2601.00001"), paper("2601.00002"), paper("cs/9901002"))
        markdown, webpage = render_daily(payload_data)
        pattern = r"arxiv\.org/abs/([^\s\)\"]+)"
        self.assertEqual(set(re.findall(pattern, markdown)), {"2601.00001", "2601.00002", "cs/9901002"})
        self.assertEqual(set(re.findall(pattern, webpage)), set(re.findall(pattern, markdown)))
        for identifier in ("2601.00001", "2601.00002", "cs/9901002"):
            self.assertIn('id="' + anchor(identifier) + '"', webpage)

    def test_company_badges_appear_without_unknown_placeholders(self):
        markdown, webpage = render_daily(payload(paper(companies=["Kuaishou", "Meta"])))
        for output in (markdown, webpage):
            self.assertIn("Kuaishou", output)
            self.assertIn("Meta", output)
            self.assertNotIn("未知", output)
            self.assertNotIn("机构待核验", output)
        self.assertIn('class="pill"', webpage)

    def test_rebuild_reports_builds_the_index_once_for_many_editions(self):
        connection = connect(self.directory / "papers.sqlite3")
        self.addCleanup(connection.close)
        with connection:
            for day in ("2026-10-21", "2026-10-22", "2026-10-23"):
                connection.execute("INSERT INTO editions VALUES (?,?)",
                                   (day, dumps(payload(paper(), day=day))))
        with patch("paper_pipeline.publishing.publish_index", wraps=publish_index) as index:
            result = rebuild_reports(connection, self.directory, CONFIG)
        self.assertEqual(result["editions_rebuilt"], 3)
        self.assertEqual(index.call_count, 1)
        self.assertTrue(Path(result["archive"]).is_file())
        for day in ("2026-10-21", "2026-10-22", "2026-10-23"):
            self.assertIn(day, Path(result["archive"]).read_text())
            self.assertTrue((self.directory / "site" / (day + ".html")).is_file())


if __name__ == "__main__":
    unittest.main()
