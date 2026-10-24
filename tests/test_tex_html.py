"""Source-based HTML checks, including a real optional LaTeXML integration."""
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
import fitz

from paper_pipeline.tex_html import XML_ID, _bibliography_inputs, _figure_asset, _normalize_html_tables, _normalize_subfigure_wrappers, _prepare_bindings, _tools, convert_tex


class TexHtmlTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.assets = self.root / "assets"
        self.build = self.root / "build"
        for directory in (self.source, self.assets, self.build):
            directory.mkdir()

    def make_figure(self):
        with fitz.open() as document:
            page = document.new_page(width=220, height=140)
            page.draw_rect(fitz.Rect(30, 30, 190, 110), color=(1, 0, 0))
            page.insert_text((50, 80), "SOURCE FIGURE")
            document.save(self.source / "figure.pdf")

    def test_source_figure_pdf_becomes_svg_and_respects_tex_trim(self):
        self.make_figure()
        node = ET.Element("graphics", {"candidates": "figure.pdf", "graphic": "figure.pdf",
                          "options": "trim=10pt 10pt 10pt 10pt,clip=true", XML_ID: "F1.g1"})
        url = _figure_asset(node, self.source, self.assets)
        target = self.root / url
        self.assertTrue(target.is_file())
        self.assertEqual(target.suffix, ".svg")
        svg = ET.parse(target).getroot()
        width = float(svg.get("width").removesuffix("pt"))
        self.assertLess(width, 220)
        self.assertGreater(width, 195)
        self.assertTrue(svg.findall(".//{http://www.w3.org/2000/svg}path"))

    def test_graphics_must_resolve_inside_project(self):
        outside = self.root / "outside.png"
        outside.write_bytes(b"not a project asset")
        node = ET.Element("graphics", {"candidates": "../outside.png", "graphic": "../outside.png"})
        with self.assertRaisesRegex(ValueError, "图资源不存在"):
            _figure_asset(node, self.source, self.assets)

    def test_rotated_pdf_trim_uses_display_coordinates(self):
        self.make_figure()
        with fitz.open(self.source / "figure.pdf") as original:
            original[0].set_rotation(90)
            original.save(self.source / "rotated.pdf")
        node = ET.Element("graphics", {"candidates": "rotated.pdf", "options": "trim=10pt 20pt 10pt 20pt,clip=true"})
        url = _figure_asset(node, self.source, self.assets)
        svg = ET.parse(self.root / url).getroot()
        self.assertAlmostEqual(float(svg.get("width").removesuffix("pt")), 140 - 20 * 72 / 72.27, places=2)
        self.assertAlmostEqual(float(svg.get("height").removesuffix("pt")), 220 - 40 * 72 / 72.27, places=2)

    def test_subfigure_layout_fix_preserves_content_ids_and_processing_instructions(self):
        xml = self.build / "document.xml"
        xml.write_text('<?xml version="1.0"?><?latexml class="acmart"?>'
                       '<document xmlns="http://dlmf.nist.gov/LaTeXML"><figure xml:id="F1">'
                       '<block><figure xml:id="F1.a"><caption>子图</caption></figure></block>'
                       '</figure><block class="keep"><figure xml:id="F2"/></block></document>')
        self.assertEqual(_normalize_subfigure_wrappers(xml), 1)
        content = xml.read_text()
        self.assertIn('<?latexml class="acmart"?>', content)
        self.assertIn('xml:id="F1.a"', content)
        self.assertIn("子图", content)
        self.assertIn('<block class="keep">', content)

    def test_adjustbox_tables_preserve_merged_cells_and_math(self):
        soup = BeautifulSoup('<article><p id="results"><span><span class="ltx_tabular">'
                             '<span class="ltx_tr"><span class="ltx_td ltx_colspan_2">Score</span></span>'
                             '<span class="ltx_tr"><span class="ltx_td ltx_rowspan_2">0.87'
                             '<math><mi>x</mi></math></span></span></span></span></p></article>', 'html.parser')
        original_text = soup.get_text()
        self.assertEqual(_normalize_html_tables(soup.article), 1)
        self.assertEqual(soup.get_text(), original_text)
        self.assertEqual(soup.select_one("td[colspan]")["colspan"], "2")
        self.assertEqual(soup.select_one("td[rowspan]")["rowspan"], "2")
        self.assertEqual(str(soup.math), "<math><mi>x</mi></math>")
        self.assertEqual(soup.find(id="results").name, "div")
        self.assertIsNone(soup.table.find_parent("span"))

    def test_missing_converter_is_explicit_not_a_pdf_fallback(self):
        with patch.dict(os.environ, {}, clear=True), patch("shutil.which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "LaTeXML"):
                _tools(None)

    def test_compatibility_does_not_silently_drop_active_dummy_commands(self):
        (self.source / "googledeepmind.cls").write_text("class fixture")
        (self.source / "main.tex").write_text(r"\documentclass{article}\begin{document}\lipsum[1]\end{document}")
        with self.assertRaisesRegex(ValueError, "实际用于正文"):
            _prepare_bindings(self.source, self.build)

    def test_paper_specific_adapter_survives_standard_binding_refresh(self):
        (self.source / "googledeepmind.cls").write_text("fixture")
        overrides = self.build / "bindings-overrides"
        overrides.mkdir()
        adapter = "# paper-specific optional argument support\n1;\n"
        (overrides / "tcolorbox.sty.ltxml").write_text(adapter)
        for _ in range(2):
            bindings = _prepare_bindings(self.source, self.build)
            self.assertEqual((bindings / "tcolorbox.sty.ltxml").read_text(), adapter)

    def test_bibliography_month_fix_preserves_source_and_defined_macros(self):
        original = '@article{a, title={Paper}, month=July, year={2025}}\n'
        original += '@string{August="Late August"}\n@article{b, month=August}\n'
        original += '@dataset{dataset_key, title={Original Dataset}, howpublished={https://example.org/data}}\n'
        bibliography = self.source / "refs.bib"
        bibliography.write_text(original)
        xml = ET.fromstring('<document xmlns="http://dlmf.nist.gov/LaTeXML">'
                            '<bibliography files="refs"/></document>')
        args = _bibliography_inputs(xml, self.source, self.build)
        self.assertEqual(bibliography.read_text(), original)
        normalized = Path(args[0].removeprefix("--bibliography=")).read_text()
        self.assertIn("month={July}", normalized)
        self.assertIn("month=August", normalized)
        self.assertIn("@misc{dataset_key, title={Original Dataset}, howpublished={https://example.org/data}}", normalized)

    @unittest.skipUnless(os.environ.get("LATEXML_ROOT") or shutil.which("latexmlc"), "LaTeXML not installed")
    def test_real_tex_preserves_chinese_math_tables_graphics_and_citations(self):
        self.make_figure()
        (self.source / "main.tex").write_text(r"""\documentclass{article}
\usepackage{amsmath,graphicx,booktabs}
\title{中文全文}\begin{document}\maketitle
\begin{abstract}完整中文摘要。\end{abstract}
\section{方法}\label{sec:method}
中文正文与公式 $x_1+y^2$，参考文献\cite{paper2024}。
\begin{equation}\label{eq:loss}L=\sum_{i=1}^{n} p_i\log q_i\end{equation}
见公式\eqref{eq:loss}，以及章节\ref{sec:method}。
\begin{table}\caption{实验数据}\label{tab:results}
\begin{tabular}{lr}\toprule Model & AUC \\ \midrule HSTU & 0.891 \\ \bottomrule\end{tabular}\end{table}
\begin{figure}\includegraphics[width=\linewidth]{figure.pdf}\caption{源码图形}\end{figure}
\begin{thebibliography}{1}\bibitem{paper2024}A. Author. Research Paper. 2024.\end{thebibliography}
\end{document}""", encoding="utf-8")
        result = convert_tex(self.source, "main.tex", self.assets, self.build)
        soup = BeautifulSoup(result["body_html"], "html.parser")
        self.assertEqual(result["format"], "arxiv_tex")
        self.assertIn("中文正文", soup.get_text())
        self.assertEqual(len(soup.find_all("math")), 2)
        self.assertIn("0.891", [cell.get_text(strip=True) for cell in soup.select(".ltx_tabular td")])
        self.assertEqual(len(soup.select(".ltx_bibitem")), 1)
        self.assertIn("摘要", [entry["title"] for entry in result["toc"]])
        for link in soup.select('a[href^="#"]'):
            self.assertIsNotNone(soup.find(id=link["href"][1:]))
        figure = soup.select_one(".ltx_graphics")
        self.assertTrue((self.root / figure["src"]).exists())
        self.assertTrue(figure["src"].endswith(".svg"))
        self.assertFalse(soup.select(".paper-inline-math,.paper-equation"))

    @unittest.skipUnless(os.environ.get("LATEXML_ROOT") or shutil.which("latexmlc"), "LaTeXML not installed")
    def test_real_tex_rejects_missing_reference_without_href(self):
        (self.source / "main.tex").write_text(
            r"\documentclass{article}\begin{document}中文正文。\ref{missing-label}\end{document}",
            encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "未解析的引用或资源.*missing-label"):
            convert_tex(self.source, "main.tex", self.assets, self.build)

    @unittest.skipUnless(os.environ.get("LATEXML_ROOT") or shutil.which("latexmlc"), "LaTeXML not installed")
    def test_report_compatibility_preserves_cases_answers_and_bibliography(self):
        (self.source / "googledeepmind.cls").write_text("compatibility fixture")
        (self.source / "references.bib").write_text(
            "@article{original_key,\n author={Researcher, Alice and knight_raider},\n title={Case Paper}, year={2026}}")
        (self.source / "main.tex").write_text(r"""\documentclass{article}
\usepackage{CJKutf8,tcolorbox,biblatex}
\addbibresource{references.bib}
\newtcolorbox{mycase}[1]{title=#1}
\begin{document}\begin{CJK*}{UTF8}{gbsn}
\section{案例}\begin{mycase}{中文案例标题}完整案例正文。\end{mycase}
\begin{tcolorbox}[title={关键结果},before upper={\textbf{Answer: 6}}]
推理证据 $x=6$。
\end{tcolorbox}
引用原始文献\textcite{original_key}。\printbibliography
\end{CJK*}\end{document}""", encoding="utf-8")
        original_files = {path.name: path.read_bytes() for path in self.source.iterdir()}
        result = convert_tex(self.source, "main.tex", self.assets, self.build)
        self.assertEqual(original_files, {path.name: path.read_bytes() for path in self.source.iterdir()})
        soup = BeautifulSoup(result["body_html"], "html.parser")
        text = soup.get_text(" ", strip=True)
        for expected in ("中文案例标题", "完整案例正文", "关键结果", "Answer: 6", "推理证据", "Case paper", "knight_raider"):
            self.assertIn(expected, text)
        self.assertEqual(len(soup.select(".ltx_framed")), 2)
        self.assertEqual(len(soup.select(".ltx_bibitem")), 1)
        xml = ET.parse(self.build / "document.xml").getroot()
        self.assertEqual(xml.find(".//{http://dlmf.nist.gov/LaTeXML}bibref").get("bibrefs"), "original_key")
        target = "#" + soup.select_one(".ltx_bibitem")["id"]
        self.assertIsNotNone(soup.find("a", href=target))


if __name__ == "__main__":
    unittest.main()
