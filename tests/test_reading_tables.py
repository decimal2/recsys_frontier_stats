import unittest

from bs4 import BeautifulSoup

from paper_pipeline.reading_tables import normalize_reading_tables


class ReadingTablesTests(unittest.TestCase):
    def parse(self, html):
        return BeautifulSoup("<article>" + html + "</article>", "html.parser").article

    def test_span_table_preserves_values_spans_math_and_links(self):
        article = self.parse('<figure id="results"><p id="box"><span class="ltx_tabular">'
            '<span class="ltx_thead"><span class="ltx_tr"><span class="ltx_td ltx_th ltx_colspan_2">'
            'AUC <a href="#reference">[8]</a></span></span></span>'
            '<span class="ltx_tbody"><span class="ltx_tr"><span class="ltx_td ltx_rowspan_2">0.7965</span>'
            '<span class="ltx_td"><math><mn>120.2</mn></math>B</span></span>'
            '<span class="ltx_tr"><span class="ltx_td">13.6%</span></span></span></span></p>'
            '<figcaption>完整表注</figcaption></figure><p id="reference">Reference</p>')
        original_text = article.get_text()
        original_math = str(article.math)
        counts = normalize_reading_tables(article)
        self.assertEqual(counts["span_tables"], 1)
        self.assertEqual(article.get_text(), original_text)
        self.assertEqual(str(article.math), original_math)
        self.assertEqual(article.select_one("thead th")["colspan"], "2")
        self.assertEqual(article.select_one("tbody td")["rowspan"], "2")
        self.assertEqual(article.select_one("a")["href"], "#reference")
        self.assertEqual(article.find(id="box").name, "div")
        self.assertEqual(article.figcaption.parent.get("id"), "results")
        self.assertIsNone(article.figcaption.find_parent(class_="paper-table-scroll"))

    def test_nested_header_table_does_not_get_a_second_scroll_container(self):
        article = self.parse('<table class="ltx_tabular"><tr><th class="ltx_th">'
            '<span class="ltx_tabular"><span class="ltx_tr"><span class="ltx_td">MMLU</span></span>'
            '<span class="ltx_tr"><span class="ltx_td">STEM</span></span></span></th>'
            '<td class="ltx_td">19.5%</td></tr></table>')
        normalize_reading_tables(article)
        self.assertEqual(len(article.select("table")), 2)
        self.assertEqual(len(article.select(".paper-table-scroll")), 1)
        self.assertEqual(article.select_one("th table tr td").get_text(), "MMLU")
        self.assertEqual(article.select_one("th").name, "th")

    def test_tex_box_height_and_scaling_do_not_bound_reflowed_table(self):
        article = self.parse('<figure id="S2.T1"><div class="ltx_transformed_outer" '
            'style="width:433.6pt;height:137.1pt;vertical-align:-0.9pt;color:red;">'
            '<span id="inner" class="ltx_transformed_inner" '
            'style="transform:translate(-21pt,6.6pt) scale(0.91);">'
            '<table class="ltx_tabular"><tr><td class="ltx_td">120.2B</td>'
            '<td class="ltx_td">13.6%</td></tr></table></span></div>'
            '<figcaption>Caption <a href="#ref">2026</a></figcaption></figure>'
            '<div class="ltx_transformed_outer" style="height:20pt;">Unrelated figure</div>')
        original_text = article.get_text()
        counts = normalize_reading_tables(article)
        self.assertEqual(counts["layout_wrappers"], 2)
        self.assertEqual(article.get_text(), original_text)
        self.assertEqual(article.select_one(".paper-table-layout")["style"], "color:red;")
        self.assertNotIn("style", article.find(id="inner").attrs)
        self.assertEqual(article.find(id="inner").name, "div")
        self.assertEqual(article.select(".ltx_transformed_outer")[-1]["style"], "height:20pt;")
        self.assertEqual(article.figcaption.parent.get("id"), "S2.T1")
        first_pass = str(article)
        self.assertEqual(normalize_reading_tables(article),
                         {"span_tables": 0, "layout_wrappers": 0, "scroll_wrappers": 0})
        self.assertEqual(str(article), first_pass)

    def test_minipage_keeps_its_caption_id_and_non_layout_styles(self):
        article = self.parse('<figure id="panel" class="ltx_figure_panel ltx_minipage" '
            'style="width:208.1pt;font-size:90%;"><table class="ltx_tabular">'
            '<tr><td class="ltx_td" colspan="3">same text</td></tr></table>'
            '<figcaption>Panel caption</figcaption></figure>')
        normalize_reading_tables(article)
        self.assertEqual(article.figure["style"], "font-size:90%;")
        self.assertEqual(article.figure["id"], "panel")
        self.assertEqual(article.td["colspan"], "3")
        self.assertEqual(article.figcaption.get_text(), "Panel caption")

    def test_underlined_results_and_named_models_keep_their_exact_content(self):
        article = self.parse('<figure><figcaption>Second-best results are '
            '<span class="ltx_text ltx_framed ltx_framed_underline">underlined</span>.</figcaption>'
            '<table class="ltx_tabular"><tr><td class="ltx_td">OneRec-1.7B</td>'
            '<td class="ltx_td"><span class="ltx_text ltx_framed ltx_framed_underline">0.2031</span></td>'
            '<td class="ltx_td">-1.7B</td></tr></table></figure>')
        original_text = article.get_text()
        underlines = [str(node) for node in article.select('.ltx_framed_underline')]
        normalize_reading_tables(article)
        self.assertEqual(article.get_text(), original_text)
        self.assertEqual([str(node) for node in article.select('.ltx_framed_underline')], underlines)
        self.assertEqual([cell.get_text() for cell in article.select('td')], ['OneRec-1.7B', '0.2031', '-1.7B'])

    def test_highlight_ink_respects_background_and_preserves_readable_source_color(self):
        article = self.parse('<table class="ltx_tabular"><tr>'
            '<td class="ltx_td" id="light" style="background-color:#FFF5EB;">0.0548</td>'
            '<td class="ltx_td" id="dark" style="background-color:black;">0.42</td>'
            '<td class="ltx_td" id="low" style="background-color:#6CC180;color:#FFFFFF;">0.8</td>'
            '<td class="ltx_td" id="good" style="background-color:#FFFFFF;color:#700000;">0.9</td>'
            '</tr></table>')
        original_styles = {node['id']: node['style'] for node in article.select('[id]')}
        normalize_reading_tables(article)
        self.assertIn('paper-table-ink-dark', article.find(id='light')['class'])
        self.assertIn('paper-table-ink-light', article.find(id='dark')['class'])
        self.assertIn('paper-table-ink-dark', article.find(id='low')['class'])
        self.assertEqual(article.find(id='good')['class'], ['ltx_td'])
        self.assertEqual({node['id']: node['style'] for node in article.select('[id]')}, original_styles)
        first_pass = str(article)
        normalize_reading_tables(article)
        self.assertEqual(str(article), first_pass)

    def test_highlighted_math_keeps_exact_dom_and_uses_an_outer_ink_wrapper(self):
        article = self.parse('<table class="ltx_tabular"><tr><td class="ltx_td">'
            '<math id="formula" style="background-color:#D8CFC0;"><mi>x</mi><mo>+</mo><mn>1</mn></math>'
            '</td></tr></table>')
        original_math = str(article.math)
        normalize_reading_tables(article)
        self.assertEqual(str(article.math), original_math)
        self.assertIn('paper-table-math-ink', article.math.parent['class'])
        self.assertIn('paper-table-ink-dark', article.math.parent['class'])
        first_pass = str(article)
        normalize_reading_tables(article)
        self.assertEqual(str(article), first_pass)

    def test_shaded_listing_gets_readable_base_ink_and_keeps_syntax_colors(self):
        article = self.parse('<div class="ltx_listing ltx_minipage" style="background-color:#F7F7F7;">'
            '<a download="" href="data:text/plain;base64,eA==">⬇</a>'
            '<span class="ltx_lst_identifier">weighted_logp</span> = '
            '<span id="keyword" style="color:#008000;">lambda</span> '
            '<span id="number" style="color:#666666;">0.5</span>'
            '<span id="faint" style="color:#CCCCCC;">comment</span>'
            '<math><mi>x</mi></math>'
            '<svg><text style="background-color:#FFFFFF;color:#CCCCCC;">drawing</text></svg>'
            '</div>')
        original_text, original_math, original_svg = article.get_text(), str(article.math), str(article.svg)
        original_link = str(article.a)
        original_styles = [node['style'] for node in article.select('[style]')]
        normalize_reading_tables(article)
        self.assertIn('paper-table-ink-dark', article.select_one('.ltx_listing')['class'])
        self.assertNotIn('paper-table-ink-dark', article.find(id='keyword').get('class', []))
        self.assertNotIn('paper-table-ink-dark', article.find(id='number').get('class', []))
        self.assertIn('paper-table-ink-dark', article.find(id='faint')['class'])
        self.assertEqual(article.get_text(), original_text)
        self.assertEqual(str(article.math), original_math)
        self.assertEqual(str(article.svg), original_svg)
        self.assertEqual(str(article.a), original_link)
        self.assertEqual([node['style'] for node in article.select('[style]')], original_styles)
        first_pass = str(article)
        normalize_reading_tables(article)
        self.assertEqual(str(article), first_pass)

    def test_listing_without_explicit_background_keeps_original_colors(self):
        article = self.parse('<div class="ltx_listing"><span style="color:#CCCCCC;">comment</span></div>')
        original = str(article)
        normalize_reading_tables(article)
        self.assertEqual(str(article), original)


if __name__ == "__main__":
    unittest.main()
