import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paper_pipeline.cli import _main
from paper_pipeline.paper_pages import build_papers, load_catalog


class PaperPagesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        (self.source / "zh").mkdir(parents=True)
        self.output = self.root / "reports"
        self.catalog = self.root / "catalog.json"
        self.records = [dict(short_name="PaperA", arxiv_id="2510.26104", version="v3", title_zh="中文全文甲",
                             title_en="Paper A", topics=["推荐"], companies=["Meta"], zh_pdf="zh/a.pdf"),
                        dict(short_name="PaperB", arxiv_id="2402.17152", version="v3", title_zh="中文全文乙",
                             title_en="Paper B", topics=[], companies=[], zh_pdf="zh/b.pdf")]
        self.catalog.write_text(json.dumps({"papers": self.records}))
        (self.source / "zh/a.pdf").write_bytes(b"first source")
        self.calls = []

    def convert(self, pdf, assets):
        self.calls.append(pdf.name)
        (assets / "figure.png").write_bytes(b"fixture image")
        return {"body_html": '<h2 id="intro">引言</h2><p>中文全文测试。</p><img src="assets/figure.png" alt="图1">',
                "toc": [{"id": "intro", "title": "引言", "level": 1}], "stats": {"pages": 1}}

    def build(self, **kwargs):
        with patch("paper_pipeline.paper_html.convert_pdf", side_effect=self.convert):
            return build_papers(self.source, self.output, self.catalog, **kwargs)

    def test_build_skips_missing_chinese_and_does_not_touch_daily(self):
        daily = self.output / "site/2026-10-24.html"
        daily.parent.mkdir(parents=True)
        daily.write_text("saved daily")
        result = self.build()
        self.assertEqual(result["built"], ["2510.26104"])
        self.assertEqual(result["skipped"], ["2402.17152"])
        self.assertEqual(daily.read_text(), "saved daily")
        page = (daily.parent / "papers/2510.26104/index.html").read_text()
        self.assertIn('href="https://arxiv.org/abs/2510.26104v3"', page)
        self.assertIn('href="#intro"', page)
        self.assertIn('href="../../daily/index.html"', page)
        self.assertIn("中文全文测试", page)
        self.assertIn("PaperA", Path(result["homepage"]).read_text())
        self.assertNotIn("PaperB", Path(result["library"]).read_text())

    def test_cache_rebuilds_changed_sources_and_missing_assets(self):
        self.build(identifiers=["PaperA"])
        result = self.build(identifiers=["2510.26104"])
        self.assertEqual(result["cached"], ["2510.26104"])
        self.assertEqual(self.calls, ["a.pdf"])
        (self.source / "zh/a.pdf").write_bytes(b"changed source")
        self.build(identifiers=["PaperA"])
        self.assertEqual(len(self.calls), 2)
        (self.output / "site/papers/2510.26104/assets/figure.png").unlink()
        self.build(identifiers=["PaperA"])
        self.assertEqual(len(self.calls), 3)

    def test_selective_build_retains_other_papers(self):
        (self.source / "zh/b.pdf").write_bytes(b"second source")
        self.build()
        result = self.build(identifiers=["PaperA"])
        self.assertEqual(result["papers"], 2)
        self.assertIn("PaperB", Path(result["library"]).read_text())
        with self.assertRaisesRegex(ValueError, "论文目录中没有"):
            self.build(identifiers=["Unknown"])

    def test_catalog_prevents_duplicate_ids_and_source_escape(self):
        self.records[1]["arxiv_id"] = self.records[0]["arxiv_id"]
        self.catalog.write_text(json.dumps({"papers": self.records}))
        with self.assertRaisesRegex(ValueError, "重复或无效"):
            load_catalog(self.catalog)
        self.records[0]["zh_pdf"] = "../../outside.pdf"
        self.catalog.write_text(json.dumps({"papers": self.records[:1]}))
        with self.assertRaisesRegex(ValueError, "必须位于"):
            self.build()

    def test_cli_is_offline_and_does_not_create_database(self):
        db = self.root / "unused.sqlite3"
        with patch("paper_pipeline.cli.load_config", side_effect=AssertionError("must not load LLM config")), \
             patch("paper_pipeline.paper_html.convert_pdf", side_effect=self.convert), \
             contextlib.redirect_stdout(io.StringIO()):
            result = _main(["--db", str(db), "--output-dir", str(self.output), "papers", "--source-dir", str(self.source),
                            "--catalog", str(self.catalog), "--paper", "PaperA"])
        self.assertEqual(result, 0)
        self.assertFalse(db.exists())

    def test_tex_source_uses_semantic_converter_and_tracks_includes(self):
        project = self.root / "translated"
        project.mkdir()
        (project / "main.tex").write_text(r"\input{body}")
        (project / "body.tex").write_text("中文正文")
        record = {**self.records[0], "tex_project": str(project), "tex_entry": "main.tex"}
        self.catalog.write_text(json.dumps({"papers": [record]}))
        def convert(source, entry, assets, build_dir, latexml_root):
            (assets / "figure.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
            return {"format": "arxiv_tex", "body_html": '<h2 id="intro">引言</h2><math><mi>x</mi></math><table><tr><td>0.25</td></tr></table>',
                    "toc": [{"id": "intro", "title": "引言", "level": 1}], "stats": {"mathml": 1},
                    "extra_head": '<style>.tex-paper{line-height:1.8}</style>'}
        with patch("paper_pipeline.tex_html.convert_tex", side_effect=convert) as renderer, \
             patch("paper_pipeline.paper_html.convert_pdf", side_effect=AssertionError("must not extract PDF")):
            result = build_papers(self.source, self.output, self.catalog)
            self.assertEqual(result["built"], ["2510.26104"])
            html = (self.output / "site/papers/2510.26104/index.html").read_text()
            self.assertIn("<math>", html)
            self.assertIn("<table>", html)
            self.assertIn("已有中文 PDF", html)
            self.assertNotIn("页中文原稿", html)
            self.assertEqual(build_papers(self.source, self.output, self.catalog)["cached"], ["2510.26104"])
            (self.source / record["zh_pdf"]).write_bytes(b"updated PDF, unchanged TeX")
            self.assertEqual(build_papers(self.source, self.output, self.catalog)["cached"], ["2510.26104"])
            self.assertEqual((self.output / "site/papers/2510.26104/paper.zh.pdf").read_bytes(),
                             b"updated PDF, unchanged TeX")
            self.assertEqual(renderer.call_count, 1)
            (project / "body.tex").write_text("中文正文已更新")
            self.assertEqual(build_papers(self.source, self.output, self.catalog)["built"], ["2510.26104"])
            self.assertEqual(renderer.call_count, 2)
        public = (self.output / "site/papers/catalog.json").read_text()
        self.assertNotIn(str(project), public)

    def test_missing_tex_source_never_silently_falls_back_to_pdf(self):
        self.records[0].update(tex_project=str(self.root / "missing"), tex_entry="main.tex")
        self.catalog.write_text(json.dumps({"papers": self.records[:1]}))
        with patch("paper_pipeline.paper_html.convert_pdf", side_effect=AssertionError("must not fall back")):
            with self.assertRaisesRegex(ValueError, "找不到中文 TeX 工程"):
                build_papers(self.source, self.output, self.catalog)


if __name__ == "__main__":
    unittest.main()
