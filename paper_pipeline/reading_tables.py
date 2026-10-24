"""Adapt TeX table layout to a flowing HTML reader without changing its data."""
import re

from bs4 import BeautifulSoup, NavigableString


TABLE_STYLES = """
.paper-fulltext .paper-table-layout{display:block;max-width:100%;min-width:0}
.paper-fulltext .paper-table-scroll{display:block;max-width:100%;overflow-x:auto;overflow-y:hidden;margin:12px 0;padding:2px 0}
.paper-fulltext .paper-table-scroll>.ltx_tabular{display:table;width:auto;max-width:none;overflow:visible;table-layout:auto;margin:0 auto}
.paper-fulltext .ltx_tabular,.paper-fulltext .ltx_tabular td,.paper-fulltext .ltx_tabular th{overflow-wrap:normal;word-break:normal}
.paper-fulltext figure.ltx_table{overflow:visible}
.paper-fulltext figure.ltx_table a{display:inline}
.paper-fulltext .ltx_tabular .ltx_tabular{margin:0 auto}
"""

_TABLE_TAGS = (("ltx_tabular", "table"), ("ltx_thead", "thead"),
               ("ltx_tbody", "tbody"), ("ltx_tfoot", "tfoot"),
               ("ltx_tr", "tr"), ("ltx_th", "th"), ("ltx_td", "td"))
_LAYOUT_CLASSES = {"ltx_transformed_outer", "ltx_transformed_inner", "ltx_parbox", "ltx_minipage"}
_LAYOUT_PROPERTIES = {"width", "height", "min-width", "max-width", "min-height", "max-height",
                      "transform", "transform-origin", "vertical-align"}


def _span_value(cell, attribute):
    if cell.has_attr(attribute):
        return str(cell[attribute])
    for name in cell.get("class", []):
        match = re.fullmatch(r"ltx_" + attribute + r"_(\d+)", name)
        if match:
            return match[1]
    return "1"


def _protected_content(article):
    """Compare actual values, ordering, math markup and links, not just counts."""
    cells = article.select(".ltx_td,.ltx_th")
    return {
        "text": tuple(str(node) for node in article.descendants if isinstance(node, NavigableString)),
        "math": tuple(str(node) for node in article.find_all("math")),
        "ids": tuple(node["id"] for node in article.select("[id]")),
        "links": tuple((node.get("href"), tuple(sorted((key, str(value)) for key, value in node.attrs.items())))
                       for node in article.find_all("a")),
        "images": tuple(str(node) for node in article.find_all("img")),
        "cells": tuple((node.get_text(), _span_value(node, "rowspan"), _span_value(node, "colspan"))
                       for node in cells),
    }


def normalize_reading_tables(article):
    """Normalize a BeautifulSoup Tag in place; retain all text and table content.

    The fixed TeX box heights describe the original PDF font metrics. Keeping
    them after browser reflow puts captions over later table rows. Remove only
    those layout properties, preserve the containers and their IDs, and scroll
    each outermost table independently of its caption. Nested header tables
    remain part of the containing cell.
    """
    before = _protected_content(article)
    tables = article.select(".ltx_tabular")
    counts = {"span_tables": sum(table.name == "span" for table in tables),
              "layout_wrappers": 0, "scroll_wrappers": 0}
    for table in tables:
        for node in [table, *table.find_all(True)]:
            if node.name == "span":
                for class_name, tag_name in _TABLE_TAGS:
                    if class_name in node.get("class", []):
                        node.name = tag_name
                        break
            if node.name in {"td", "th"}:
                for attribute in ("rowspan", "colspan"):
                    value = _span_value(node, attribute)
                    if value != "1" and not node.has_attr(attribute):
                        node[attribute] = value
        # A table is block content: remove invalid span/p ancestors while
        # retaining their text, styles, identifiers and other children.
        for parent in table.parents:
            if parent.name in {"td", "th", "figure", "article", "section"}:
                break
            if parent.name in {"span", "p"}:
                parent.name = "div"

    for node in article.find_all(True):
        classes = node.get("class", [])
        if not _LAYOUT_CLASSES.intersection(classes) or node.select_one(".ltx_tabular") is None:
            continue
        original = (node.name, node.get("style"), tuple(classes))
        if node.name in {"span", "p"}:
            node.name = "div"
        if node.has_attr("style"):
            kept = [part for part in node["style"].split(";")
                    if part.strip() and part.split(":", 1)[0].strip().lower() not in _LAYOUT_PROPERTIES]
            if kept:
                node["style"] = ";".join(kept) + ";"
            else:
                del node["style"]
        if "paper-table-layout" not in classes:
            node["class"] = [*classes, "paper-table-layout"]
        if original != (node.name, node.get("style"), tuple(node.get("class", []))):
            counts["layout_wrappers"] += 1

    factory = BeautifulSoup("", "html.parser")
    for table in tables:
        if table.find_parent(class_="ltx_tabular") is not None:
            continue
        if table.find_parent(class_="paper-table-scroll") is not None:
            continue
        wrapper = factory.new_tag("div", attrs={"class": "paper-table-scroll", "tabindex": "0",
                                                "role": "region", "aria-label": "可横向滚动的论文表格"})
        table.wrap(wrapper)
        counts["scroll_wrappers"] += 1

    after = _protected_content(article)
    for field, original in before.items():
        if after[field] != original:
            raise ValueError("论文表格布局规范化改变了受保护内容：" + field)
    return counts
