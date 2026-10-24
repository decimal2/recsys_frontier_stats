"""Offline conversion checks with generated PDFs, including actual crop geometry."""
from pathlib import Path
import tempfile
import unittest

import fitz
from bs4 import BeautifulSoup

from paper_pipeline.paper_html import convert_pdf


class PaperHtmlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def convert(self, document):
        source = self.root / "paper.pdf"
        document.save(source)
        document.close()
        return convert_pdf(source, self.root / "assets")

    def test_table_crop_contains_all_rows_not_just_a_repeated_rule(self):
        document = fitz.open()
        page = document.new_page(width=600, height=800)
        page.insert_text((70, 90), "Table 1: Offline results", fontsize=11)
        shape = page.new_shape()
        # A single drawing path deliberately contains all horizontal rules.
        for y in (110, 135, 180):
            shape.draw_line((70, y), (520, y))
        shape.finish(color=(0, 0, 0))
        shape.commit()
        page.insert_text((80, 127), "Method                         Value", fontsize=11)
        page.insert_text((80, 158), "HSTU                              0.25", fontsize=11)
        page.insert_text((70, 230), "Body after the table <script>example</script>", fontsize=11)
        result = self.convert(document)
        soup = BeautifulSoup(result["body_html"], "html.parser")
        table = soup.select_one(".paper-table img")
        self.assertIsNotNone(table)
        image = fitz.Pixmap(self.root / table["src"])
        self.assertGreater(image.height, 150)
        self.assertIn("Body after the table", soup.get_text())
        self.assertIsNone(soup.find("script"))
        self.assertIn("&lt;script&gt;", result["body_html"])

    def test_bottom_caption_and_adjacent_top_caption_do_not_share_table(self):
        document = fitz.open()
        page = document.new_page(width=600, height=800)
        for y in (110, 135, 180, 250, 275, 320):
            page.draw_line((70, y), (520, y))
        page.insert_text((80, 158), "FIRST TABLE ROW", fontsize=11)
        page.insert_text((70, 200), "Table 1: Caption below table", fontsize=11)
        page.insert_text((70, 235), "Table 2: Caption above table", fontsize=11)
        page.insert_text((80, 300), "SECOND TABLE ROW", fontsize=11)
        result = self.convert(document)
        soup = BeautifulSoup(result["body_html"], "html.parser")
        figures = soup.select(".paper-table")
        self.assertEqual([f["id"] for f in figures], ["table-1", "table-2"])
        for figure in figures:
            image = fitz.Pixmap(self.root / figure.img["src"])
            self.assertGreater(image.height, 150)
            self.assertLess(image.height, 210)

    def test_english_reference_columns_are_read_left_then_right(self):
        document = fitz.open()
        page = document.new_page(width=600, height=800)
        # Deliberately insert right-column text first. A y-only sort would put
        # RIGHT_FIRST before the continuation at the foot of the left column.
        for x, y, marker in [(320, 90, "RIGHT_FIRST"), (50, 90, "LEFT_FIRST"),
                             (50, 350, "LEFT_LAST"), (320, 350, "RIGHT_LAST")]:
            text = marker + " " + ("A reference author. A long research title and publication details. " * 4)
            page.insert_textbox((x, y, x + 225, y + 100), text, fontsize=10)
        result = self.convert(document)
        text = BeautifulSoup(result["body_html"], "html.parser").get_text()
        self.assertLess(text.index("LEFT_LAST"), text.index("RIGHT_FIRST"))
        self.assertLess(text.index("RIGHT_FIRST"), text.index("RIGHT_LAST"))

    def test_chinese_headings_and_every_page_are_preserved(self):
        document = fitz.open()
        page = document.new_page(width=600, height=800)
        page.insert_font(fontname="china-s")
        page.insert_text((70, 100), "1", fontname="hebo", fontsize=14)
        page.insert_text((95, 100), "引言", fontname="china-s", fontsize=14)
        page.insert_text((70, 130), "这是中文全文的第一段内容。", fontname="china-s", fontsize=11)
        page = document.new_page(width=600, height=800)
        page.insert_text((70, 100), "References", fontname="hebo", fontsize=14)
        page.insert_text((70, 130), "A. Author. A research reference. 2024.", fontsize=11)
        result = self.convert(document)
        self.assertEqual(result["stats"]["pages"], 2)
        self.assertIn("这是中文全文的第一段内容。", result["body_html"])
        self.assertIn("A research reference", result["body_html"])
        self.assertIn('id="page-2"', result["body_html"])
        self.assertIn("paper.zh.pdf#page=2", result["body_html"])
        self.assertIn("参考文献", [entry["title"] for entry in result["toc"]])


if __name__ == "__main__":
    unittest.main()
