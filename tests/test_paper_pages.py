import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup

from paper_pipeline.cli import _main
from paper_pipeline.paper_pages import build_papers, load_catalog, read_html_snapshot, render_library, render_paper


class PaperPagesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "reports"
        self.catalog = self.root / "catalog.json"
        self.records = [dict(short_name="PaperA", arxiv_id="2510.26104", version="v3", title_zh="中文全文甲",
                             title_en="Paper A", topics=["推荐"], companies=["Meta"]),
                        dict(short_name="PaperB", arxiv_id="2402.17152", version="v3", title_zh="中文全文乙",
                             title_en="Paper B", topics=[], companies=[])]
        self.document = {"format": "arxiv_tex", "body_html": '<h2 id="intro">引言</h2><p>中文全文测试。</p>'
                         '<math><mi>x</mi></math><img src="assets/figure.svg" alt="图1">',
                         "toc": [{"id": "intro", "title": "引言", "level": 1}], "stats": {"mathml": 1},
                         "extra_head": "<style>.tex-paper{line-height:1.8}</style>"}
        for paper in self.records:
            paper["html_snapshot"] = f"docs/papers/{paper['arxiv_id']}/index.html"
            snapshot = self.root / paper["html_snapshot"]
            (snapshot.parent / "assets").mkdir(parents=True)
            (snapshot.parent / "assets/figure.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
            snapshot.write_text(render_paper({**paper, "arxiv_url": "https://arxiv.org/abs/" + paper["arxiv_id"] + paper["version"]}, self.document))
        self.catalog.write_text(json.dumps({"papers": self.records}))
        self.calls = []

    def read(self, snapshot, assets):
        self.calls.append(snapshot.parent.name)
        return read_html_snapshot(snapshot, assets)

    def build(self, **kwargs):
        with patch("paper_pipeline.paper_pages.read_html_snapshot", side_effect=self.read):
            return build_papers(self.output, self.catalog, **kwargs)

    def test_build_from_html_preserves_daily_and_full_paper_structure(self):
        daily = self.output / "site/2026-10-24.html"
        daily.parent.mkdir(parents=True)
        daily.write_text("saved daily")
        result = self.build()
        self.assertEqual(set(result["built"]), {"2510.26104", "2402.17152"})
        self.assertEqual(daily.read_text(), "saved daily")
        page = (daily.parent / "papers/2510.26104/index.html").read_text()
        self.assertIn('href="https://arxiv.org/abs/2510.26104v3"', page)
        self.assertIn('href="#intro"', page)
        self.assertIn('href="../../daily/index.html"', page)
        self.assertIn("中文全文测试", page)
        self.assertIn("<math>", page)
        self.assertIn(".tex-paper{line-height:1.8}", page)
        self.assertIn("PaperA", Path(result["homepage"]).read_text())
        self.assertIn("PaperB", Path(result["library"]).read_text())
        self.assertNotIn("html_snapshot", (daily.parent / "papers/catalog.json").read_text())
        self.assertFalse(list((self.output / "site").rglob("*.pdf")))

    def test_cache_rebuilds_changed_snapshots_and_missing_assets(self):
        self.build(identifiers=["PaperA"])
        result = self.build(identifiers=["2510.26104"])
        self.assertEqual(result["cached"], ["2510.26104"])
        self.assertEqual(len(self.calls), 1)
        snapshot = self.root / self.records[0]["html_snapshot"]
        snapshot.write_text(snapshot.read_text().replace("中文全文测试", "修改后的中文全文"))
        self.build(identifiers=["PaperA"])
        self.assertEqual(len(self.calls), 2)
        (self.output / "site/papers/2510.26104/assets/figure.svg").unlink()
        self.build(identifiers=["PaperA"])
        self.assertEqual(len(self.calls), 3)
        self.build(identifiers=["PaperA"], force=True)
        self.assertEqual(len(self.calls), 4)

    def test_selective_build_retains_other_papers(self):
        self.build()
        result = self.build(identifiers=["PaperA"])
        self.assertEqual(result["papers"], 2)
        self.assertIn("PaperB", Path(result["library"]).read_text())
        with self.assertRaisesRegex(ValueError, "论文目录中没有"):
            self.build(identifiers=["Unknown"])

    def test_catalog_rejects_duplicate_ids_and_external_snapshots(self):
        self.records[1]["arxiv_id"] = self.records[0]["arxiv_id"]
        self.catalog.write_text(json.dumps({"papers": self.records}))
        with self.assertRaisesRegex(ValueError, "重复或无效"):
            load_catalog(self.catalog)
        for snapshot in ("../../outside.html", str(self.root / "outside.html")):
            record = {**self.records[0], "html_snapshot": snapshot}
            self.catalog.write_text(json.dumps({"papers": [record]}))
            with self.assertRaisesRegex(ValueError, "必须位于"):
                self.build()

    def test_catalog_requires_html_and_rejects_removed_local_imports(self):
        for key in ("zh_pdf", "tex_project", "tex_entry", "latexml_root"):
            record = {**self.records[0], key: "old-local-path"}
            self.catalog.write_text(json.dumps({"papers": [record]}))
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "已删除本地 PDF/TeX 导入"):
                self.build()
        record = dict(self.records[0])
        del record["html_snapshot"]
        self.catalog.write_text(json.dumps({"papers": [record]}))
        with self.assertRaisesRegex(ValueError, "缺少 html_snapshot"):
            self.build()

    def test_cli_is_offline_and_does_not_create_database(self):
        db = self.root / "unused.sqlite3"
        with patch("paper_pipeline.cli.load_config", side_effect=AssertionError("must not load LLM config")), \
             patch("paper_pipeline.tex_html.convert_tex", side_effect=AssertionError("must not convert local TeX")), \
             contextlib.redirect_stdout(io.StringIO()):
            result = _main(["--db", str(db), "--output-dir", str(self.output), "papers",
                           "--catalog", str(self.catalog), "--paper", "PaperA"])
        self.assertEqual(result, 0)
        self.assertFalse(db.exists())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as rejected:
            _main(["papers", "--source-dir", "old-local-directory"])
        self.assertEqual(rejected.exception.code, 2)

    def test_missing_snapshot_or_asset_is_an_error(self):
        snapshot = self.root / self.records[0]["html_snapshot"]
        (snapshot.parent / "assets/figure.svg").unlink()
        with self.assertRaisesRegex(ValueError, "HTML 快照图片不存在"):
            self.build()
        snapshot.unlink()
        with self.assertRaisesRegex(ValueError, "找不到 HTML 全文快照"):
            self.build()

    def test_existing_pdf_derived_html_remains_readable_without_pdf_importer(self):
        # PPO's published HTML is a portable artifact even though its historical source was PDF.
        record = self.records[0]
        snapshot = self.root / record["html_snapshot"]
        document = {**self.document, "format": "pdf", "stats": {"pages": 3}}
        snapshot.write_text(render_paper({**record, "arxiv_url": "https://arxiv.org/abs/" + record["arxiv_id"]}, document))
        self.build(identifiers=["PaperA"])
        output = self.output / "site/papers/2510.26104"
        legacy = output / "paper_zh.pdf"
        legacy.write_bytes(b"old publication copy")
        self.build(identifiers=["PaperA"])
        self.assertFalse(legacy.exists())
        self.assertIn("中文全文测试", (output / "index.html").read_text())
        self.assertTrue((output / "assets/figure.svg").is_file())

    def test_legacy_cached_table_is_repaired_at_render_without_mutating_data(self):
        record = {**self.records[0], "arxiv_url": "https://arxiv.org/abs/2510.26104v3"}
        original = ('<article class="tex-paper"><figure class="ltx_table" id="table-1">'
                    '<span class="ltx_transformed_outer" style="height:8pt;transform:scale(.8)">'
                    '<span class="ltx_tabular"><span class="ltx_tr">'
                    '<span class="ltx_td">120.2B</span><span class="ltx_td ltx_colspan_2">'
                    '<math id="formula"><mi>x</mi></math></span></span></span></span>'
                    '<figcaption>表注 <a href="#reference">引用</a></figcaption></figure></article>')
        document = {"format": "arxiv_tex", "body_html": original, "toc": [], "stats": {}}
        html = render_paper(record, document)
        soup = BeautifulSoup(html, "html.parser")
        table = soup.select_one("table.ltx_tabular")
        self.assertEqual([cell.get_text() for cell in table.select("td")], ["120.2B", "x"])
        self.assertEqual(table.select("td")[1]["colspan"], "2")
        self.assertEqual(soup.select_one("#formula").decode(), '<math id="formula"><mi>x</mi></math>')
        self.assertIsNone(soup.select_one(".ltx_transformed_outer").get("style"))
        self.assertIsNone(soup.figcaption.find_parent(class_="paper-table-scroll"))
        self.assertEqual(document["body_html"], original)

    def test_library_defaults_to_publication_date_and_exposes_sort_metadata(self):
        records = [{**paper, "arxiv_url": "https://arxiv.org/abs/" + paper["arxiv_id"]}
                   for paper in self.records]
        # The older arXiv ID is intentionally assigned the later date: the UI
        # must use verified metadata rather than infer dates from identifiers.
        records[0]["published_date"] = "2024-01-02"
        records[1]["published_date"] = "2025-03-04"
        unknown = {**records[0], "short_name": "Unknown", "arxiv_id": "2601.12345",
                   "published_date": None}
        soup = BeautifulSoup(render_library([unknown, *records]), "html.parser")
        entries = soup.select(".paper-entry")
        self.assertEqual([entry["data-published-date"] for entry in entries],
                         ["2025-03-04", "2024-01-02", ""])
        self.assertEqual(entries[0].time["datetime"], "2025-03-04")
        self.assertEqual([option["value"] for option in soup.select("#paper-sort option")],
                         ["newest", "oldest"])
        self.assertIsNotNone(soup.select_one("#paper-topic"))
        self.assertIsNotNone(soup.select_one("#paper-search"))


if __name__ == "__main__":
    unittest.main()
