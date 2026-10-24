"""Reflow existing, born-digital Chinese paper PDFs into local HTML.

Prose remains selectable HTML. Mathematical glyph runs and complex display
objects are cropped from the *original* PDF, rather than guessed or translated.
No network or model call is made. PDF destinations are repaired by semantic
section / figure / table numbers where possible: recompiled PDFs can retain
out-of-date destinations.
"""

from collections import Counter, defaultdict
from html import escape
import hashlib
from pathlib import Path
import re

import fitz

CONVERTER_VERSION = "pdf-html-2"
# These display boundaries were visually checked against the first three source
# PDFs. Binding them to the exact source bytes prevents stale crops after edits.
_VERIFIED_REGIONS = {
    "7480859b2fa69a6682c746a2612bd28a8845ba6bc2892844fe29fb1eb7eb386d": {
        (0, "figure", 1): (330, 175, 510, 331),
        (6, "figure", 6): (335, 61, 513, 170),
        (14, "figure", 8): (55, 519, 542, 658),
        (18, "figure", 11): (102, 120, 495, 570),
        (3, "table", 2): (76, 332.5, 269, 383),
        (3, "table", 3): (327, 61, 522, 146),
        (5, "table", 4): (64, 73.5, 533, 197),
        (18, "caption-figure", 11): (55, 572, 549, 618),
    },
    "8bc8064d828c014f8fd5bd2fc1a364dc873c597afc9dcdb660f1188492a60369": {
        (5, "figure", 3): (62, 82, 540, 330),
    },
}
_VERIFIED_COLUMNS = {
    "8a33ad2ae568a4b04a64162df0266776153e235928a457841a09c0804efade84": (2, None),
    "7480859b2fa69a6682c746a2612bd28a8845ba6bc2892844fe29fb1eb7eb386d": (2, 13),
    "8bc8064d828c014f8fd5bd2fc1a364dc873c597afc9dcdb660f1188492a60369": (1, None),
}
_CAPTION = re.compile(r"^(Figure|Table|Algorithm)\s*(\d+)(?:\s*[.:|｜]|(?<=\d)\s+(?=M-FALCON))", re.I)
_NUMBERED = re.compile(r"^([A-Z](?=[.\s])|\d+)((?:\.\d+)*)\.?\s*(.+)$")
_MATH_FONT = re.compile(r"(?:Math|^CM(?:MI|SY|EX|R\d)|^tx(?:sy|ex)|^MS[AB]M|^MTMI)", re.I)
_CJK = re.compile(r"[\u3400-\u9fff]")


def _rect(value):
    return fitz.Rect(value)


def _text(line):
    parts = []
    previous = None
    for span in line["spans"]:
        if previous and span["bbox"][0] - previous["bbox"][2] > span["size"] * .5:
            parts.append(" ")
        parts.append(span["text"])
        previous = span
    return "".join(parts).strip()


def _join(lines):
    """Remove PDF line wraps without introducing spaces inside Chinese words."""
    result = ""
    for value in lines:
        if not value:
            continue
        if result and result[-1].isascii() and value[0].isascii():
            if result.endswith("-") and value[0].islower():
                result = result[:-1]
            elif not result.endswith(" "):
                result += " "
        result += value
    return result


def _normal(text):
    return re.sub(r"\s+", "", text).lower()


def _overlap(a, b):
    return max(0, min(a.x1, b.x1) - max(a.x0, b.x0))


def _union(rectangles):
    # Rect union treats zero-height horizontal rules as empty; keep their
    # coordinates explicitly or a table silently collapses to its last rule.
    return fitz.Rect(min(r.x0 for r in rectangles), min(r.y0 for r in rectangles),
                     max(r.x1 for r in rectangles), max(r.y1 for r in rectangles))


def _mostly_math(block):
    text = block["text"]
    if not text or len(_CJK.findall(text)) > 2 or len(text) > 700:
        return False
    spans = [s for line in block["lines"] for s in line["spans"]]
    math_chars = sum(len(s["text"]) for s in spans if _MATH_FONT.search(s["font"]))
    return math_chars > 0 and math_chars / max(1, len(text)) > .22


def _heading(block, toc_titles, contents_page):
    if contents_page:
        return None
    text = block["text"].strip()
    if len(text) > 130 or not text:
        return None
    normalized = _normal(text)
    if normalized in {"abstract", "摘要", "references", "参考文献", "acknowledgements", "致谢", "影响声明", "impactstatement"}:
        return (1, {"abstract": "摘要", "references": "参考文献"}.get(normalized, text))
    match = _NUMBERED.match(text)
    if not match or ". " in match[3]:
        return None
    spans = [s for line in block["lines"] for s in line["spans"] if s["text"].strip()]
    bold = sum(len(s["text"]) for s in spans if "Bold" in s["font"] or s["font"].endswith("OB"))
    # A block containing an inline subheading and its paragraph is not a heading.
    if len(block["lines"]) > 2 or (bold < len(text) * .45 and not match[2].count(".") >= 2):
        return None
    if len(text) > 85 and not any(_normal(t) in normalized for t in toc_titles):
        return None
    return (min(4, 1 + match[2].count(".")), text)


def _column_bounds(block, page, columns):
    margin = page.rect.width * .075
    middle = page.rect.width / 2
    if columns == 1 or (block["rect"].x0 < middle - 30 and block["rect"].x1 > middle + 30):
        return fitz.Rect(margin, 0, page.rect.width - margin, page.rect.height)
    if block["rect"].x0 < middle:
        return fitz.Rect(margin, 0, middle - 4, page.rect.height)
    return fitz.Rect(middle + 4, 0, page.rect.width - margin, page.rect.height)


def _column_count(blocks, page):
    prose = [b for b in blocks if len(_CJK.findall(b["text"])) > 25 and b["rect"].height > 20]
    if not prose:
        prose = [b for b in blocks if len(b["text"]) > 80 and b["rect"].height > 20]
    if not prose:
        return 1
    wide = sum(b["rect"].width > page.rect.width * .65 for b in prose)
    return 1 if wide >= len(prose) * .4 else 2


def _caption_lines(block):
    """A caption and the first table row occasionally share a PDF text block."""
    lines = []
    start = block["lines"][0]
    start_size = max(s["size"] for s in start["spans"])
    for line in block["lines"]:
        text = _text(line)
        if lines and not _CJK.search(text) and not text.endswith("-"):
            if _CJK.search(_text(lines[0])) and _text(lines[-1]).endswith("。"):
                break
            # English captions may continue; a widely separated cell row does not.
            spans = line["spans"]
            gaps = [spans[i + 1]["bbox"][0] - spans[i]["bbox"][2] for i in range(len(spans) - 1)]
            if max(gaps, default=0) > 18 or max(s["size"] for s in spans) > start_size + .8:
                break
        lines.append(line)
    return lines


def _display_regions(page, blocks, columns, verified_regions=None):
    """Find actual graphics near captions, without trusting stale PDF page links."""
    regions = []
    paths = page.get_drawings()
    drawings = [d["rect"] for d in paths]
    line_rects = []
    for path in paths:
        for item in path["items"]:
            if item[0] == "l":
                a, b = item[1], item[2]
                line_rects.append(fitz.Rect(min(a.x, b.x), min(a.y, b.y), max(a.x, b.x), max(a.y, b.y)))
            elif item[0] == "re":
                r = item[1]
                line_rects.extend([fitz.Rect(r.x0, r.y0, r.x1, r.y0), fitz.Rect(r.x0, r.y1, r.x1, r.y1)])
    images = [_rect(i["bbox"]) for i in page.get_image_info()]
    captions = [b for b in blocks if _CAPTION.match(b["text"])]
    rules = [r for r in line_rects if r.height < 1.5 and r.width > 35 and 65 < r.y0 < page.rect.height * .9]
    graphics = [r for r in drawings if r.height > 5 and r.width > 8] + images
    for block in captions:
        match = _CAPTION.match(block["text"])
        kind, number = match[1].lower(), int(match[2])
        cap_lines = _caption_lines(block)
        cap = _union([_rect(l["bbox"]) for l in cap_lines])
        # The compositor may split a long caption into adjacent PDF blocks.
        while cap_lines and not _text(cap_lines[-1]).rstrip().endswith(tuple("。.!！?？")):
            following = [b for b in blocks if b is not block and 0 <= b["rect"].y0 - cap.y1 < 6
                         and _overlap(b["rect"], cap) > min(b["rect"].width, cap.width) * .75
                         and _CJK.search(b["text"]) and not _CAPTION.match(b["text"])]
            if not following:
                break
            following = min(following, key=lambda b: b["rect"].y0)
            cap_lines.extend(following["lines"])
            cap = _union([_rect(l["bbox"]) for l in cap_lines])
        caption_key = (page.number, "caption-" + kind, number)
        if verified_regions and caption_key in verified_regions:
            caption_box = fitz.Rect(verified_regions[caption_key])
            fragments = [line for source in blocks for line in source["lines"]
                         if (caption_box & _rect(line["bbox"])).get_area() > _rect(line["bbox"]).get_area() * .8]
            cap_lines = []
            for line in sorted(fragments, key=lambda line: (line["bbox"][1], line["bbox"][0])):
                if cap_lines and abs(line["bbox"][1] - cap_lines[-1]["bbox"][1]) < 3:
                    previous = cap_lines[-1]
                    previous["spans"].extend(line["spans"])
                    previous["spans"].sort(key=lambda span: span["bbox"][0])
                    previous["bbox"] = tuple(_rect(previous["bbox"]) | _rect(line["bbox"]))
                else:
                    cap_lines.append({**line, "spans": list(line["spans"])})
            cap = _union([_rect(line["bbox"]) for line in cap_lines])
        bounds = _column_bounds(block, page, columns)
        if kind == "table" or kind == "algorithm":
            eligible = [r for r in rules if _overlap(r, bounds) > r.width * .7
                        and not any(existing["rect"].y0 - 1 <= r.y0 <= existing["rect"].y1 + 1
                                    and _overlap(existing["rect"], r) > r.width * .7 for existing in regions)]
            # Drawing paths can repeat a rule (fill + stroke); count real edges.
            eligible = list({tuple(round(v, 1) for v in r): r for r in eligible}.values())
            others = [b for b in captions if b is not block and _overlap(b["rect"], bounds) > min(b["rect"].width, bounds.width) * .4]
            low = max([b["rect"].y1 for b in others if b["rect"].y1 < cap.y0] + [60])
            high = min([b["rect"].y0 for b in others if b["rect"].y0 > cap.y1] + [page.rect.height * .9])
            groups = []
            for after in (True, False):
                side = [r for r in eligible if (cap.y1 <= r.y0 < high if after else low < r.y0 <= cap.y0)]
                if not side:
                    continue
                edge = cap.y1 if after else cap.y0
                nearest = min(side, key=lambda r: abs(r.y0 - edge))
                if abs(nearest.y0 - edge) > 100:
                    continue
                same = [r for r in side if abs(r.x0 - nearest.x0) < 10 and abs(r.x1 - nearest.x1) < 10]
                same.sort(key=lambda r: abs(r.y0 - nearest.y0))
                chosen = []
                for r in same:
                    chosen.append(r)
                if len(chosen) >= 2 and max(r.y0 for r in chosen) - min(r.y0 for r in chosen) > 8:
                    groups.append((abs(nearest.y0 - edge), chosen))
            if not groups:
                continue
            _, chosen = min(groups, key=lambda pair: pair[0])
            visual = _union(chosen)
            visual.y0 -= 2
            visual.y1 += 3
            visual.x0 -= 3
            visual.x1 += 3
            region = visual | cap
        else:
            candidates = [r for r in graphics if _overlap(r, bounds) > min(r.width, bounds.width) * .5 and r.y0 < cap.y0 and r.y1 <= cap.y1 + 5]
            if not candidates:
                continue
            nearest = min(candidates, key=lambda r: abs(cap.y0 - r.y1))
            if cap.y0 - nearest.y1 > 100:
                continue
            # Figures often have several disconnected panels. The last actual
            # prose paragraph/caption above the plot bounds the figure area.
            boundary = max([b["rect"].y1 for b in blocks if b is not block and b["rect"].y1 < nearest.y0 - 2 and _overlap(b["rect"], bounds) > min(b["rect"].width, bounds.width) * .5 and (len(_CJK.findall(b["text"])) > 15 or _CAPTION.match(b["text"]))] + [60])
            selected = [r for r in candidates if r.y0 >= boundary - 1 and r.y1 > boundary]
            visual = _union(selected or [nearest])
            # Include axis labels outside vector paths, up to the caption.
            visual.y0 = max(boundary, visual.y0 - 8)
            visual.y1 = cap.y0 - 2
            visual.x0 = max(bounds.x0, visual.x0 - 18)
            visual.x1 = min(bounds.x1, visual.x1 + 18)
            region = visual | cap
        if verified_regions and (page.number, kind, number) in verified_regions:
            visual = fitz.Rect(verified_regions[page.number, kind, number])
            region = visual | cap
        if visual.is_empty:
            continue
        regions.append({"rect": region, "visual": visual, "kind": kind, "number": number, "caption_lines": cap_lines, "text": _join([_text(l) for l in cap_lines]), "id": f"{kind}-{number}"})
    # Preserve other embedded raster illustrations, even without a caption.
    for image_rect in images:
        if image_rect.width < 8 or image_rect.height < 8:
            continue
        if any((r["rect"] & image_rect).get_area() > image_rect.get_area() * .65 for r in regions):
            continue
        regions.append({"rect": image_rect, "visual": image_rect, "kind": "image", "number": None, "caption_lines": [], "text": "论文插图", "id": None})
    # Display equations: retain mathematical layout, including numbered margins.
    available = [b for b in blocks if not any((r["rect"] & b["rect"]).get_area() > b["rect"].get_area() * .5 for r in regions)]
    math_blocks = [b for b in available if _mostly_math(b)]
    for block in math_blocks:
        if any((r["rect"] & block["rect"]).get_area() > block["rect"].get_area() * .5 for r in regions):
            continue
        bounds = _column_bounds(block, page, columns)
        chosen = [block]
        current = block["rect"]
        for other in available:
            if other is block or _overlap(other["rect"], bounds) < other["rect"].width * .7:
                continue
            only_number = bool(re.fullmatch(r"\(\d+\)", other["text"].strip()))
            if (only_number and other["rect"].y0 < current.y1 + 4 and other["rect"].y1 > current.y0 - 4) or (_mostly_math(other) and -3 < other["rect"].y0 - current.y1 < 6):
                chosen.append(other)
                current |= other["rect"]
        nums = re.findall(r"\((\d+)\)\s*$", " ".join(b["text"] for b in chosen))
        number = int(nums[-1]) if nums else None
        regions.append({"rect": current, "visual": current + (-2, -2, 2, 2), "kind": "equation", "number": number, "caption_lines": [], "text": _join([b["text"] for b in chosen]), "id": f"equation-{number}" if number else None})
    return regions


def _reading_order(items, page, columns):
    if columns == 1:
        return sorted(items, key=lambda b: (round(b["rect"].y0, 1), b["rect"].x0))
    middle = page.rect.width / 2
    wide = sorted([b for b in items if b["rect"].x0 < middle - 30 and b["rect"].x1 > middle + 30], key=lambda b: b["rect"].y0)
    narrow = [b for b in items if b not in wide]
    result = []
    for barrier in wide:
        before = [b for b in narrow if b["rect"].y0 < barrier["rect"].y0]
        result.extend(sorted(before, key=lambda b: (b["rect"].x0 >= middle, b["rect"].y0, b["rect"].x0)))
        narrow = [b for b in narrow if b not in before]
        result.append(barrier)
    result.extend(sorted(narrow, key=lambda b: (b["rect"].x0 >= middle, b["rect"].y0, b["rect"].x0)))
    return result


class _Renderer:
    def __init__(self, asset_dir, destinations):
        self.asset_dir = Path(asset_dir)
        self.asset_dir.mkdir(parents=True, exist_ok=True)
        self.destinations = destinations
        self.asset_count = 0
        self.inline_count = 0
        self.link_count = 0

    def crop(self, page, rect, label):
        rect = _rect(rect) & page.rect
        if rect.is_empty:
            return ""
        self.asset_count += 1
        filename = f"p{page.number + 1:03d}-{self.asset_count:05d}-{label}.png"
        page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5), clip=rect, alpha=False).save(self.asset_dir / filename)
        return "assets/" + filename

    def line(self, page, line, links):
        spans = line["spans"]
        normal_sizes = [s["size"] for s in spans if not _MATH_FONT.search(s["font"]) and len(s["text"].strip()) > 1]
        size = max(normal_sizes or [s["size"] for s in spans])
        baseline = max((s.get("origin", (0, 0))[1] for s in spans if s["size"] >= size * .9), default=0)
        output = []
        for span in spans:
            text = span["text"]
            if not text:
                continue
            rect = _rect(span["bbox"])
            content = escape(text)
            if _MATH_FONT.search(span["font"]) and text.strip():
                src = self.crop(page, rect + (-.25, -.25, .25, .25), "inline")
                self.inline_count += 1
                content = f'<img class="paper-inline-math" src="{src}" alt="{escape(text, quote=True)}" loading="lazy" style="width:{rect.width / size:.3f}em;height:{rect.height / size:.3f}em;vertical-align:{-(rect.y1 - baseline) / size:.3f}em">'
            elif span["size"] < size * .85 and text.strip():
                origin = span.get("origin", (0, baseline))[1]
                tag = "sup" if origin < baseline - size * .12 else "sub"
                content = f"<{tag}>{content}</{tag}>"
            else:
                if "Bold" in span["font"] or span["font"].endswith("OB"):
                    content = f"<strong>{content}</strong>"
                if "Italic" in span["font"] or span["font"].endswith("OI"):
                    content = f"<em>{content}</em>"
            for link in links:
                link_rect = link["from"]
                if rect.get_area() == 0 or (rect & link_rect).get_area() < rect.get_area() * .35:
                    continue
                href = None
                if link.get("nameddest") in self.destinations:
                    href = "#" + self.destinations[link["nameddest"]]
                elif link.get("uri", "").startswith(("https://", "http://", "mailto:")):
                    href = link["uri"]
                if href:
                    content = f'<a href="{escape(href, quote=True)}">{content}</a>'
                    self.link_count += 1
                    break
            output.append(content)
        return "".join(output)

    def paragraph(self, page, lines, links):
        output = []
        previous = ""
        for line in lines:
            value = _text(line)
            if output and previous and value and previous[-1].isascii() and value[0].isascii():
                output.append(" ")
            output.append(self.line(page, line, links))
            previous = value
        return "".join(output)


def convert_pdf(pdf_path, asset_dir):
    """Return ``{body_html, toc, stats}``; assets use relative ``assets/`` URLs."""
    source_hash = hashlib.sha256(Path(pdf_path).read_bytes()).hexdigest()
    verified_regions = _VERIFIED_REGIONS.get(source_hash, {})
    document = fitz.open(pdf_path)
    pages = []
    toc_titles = [row[1] for row in document.get_toc()]
    repeated_headers = Counter()
    for page in document:
        data = page.get_text("dict", flags=fitz.TEXT_PRESERVE_LIGATURES | fitz.TEXT_PRESERVE_WHITESPACE)
        blocks = []
        for b in data["blocks"]:
            if b["type"] != 0:
                continue
            block = {"rect": _rect(b["bbox"]), "lines": b["lines"], "text": _join([_text(l) for l in b["lines"]])}
            if block["rect"].y1 < page.rect.height * .1:
                repeated_headers[block["text"]] += 1
            blocks.append(block)
        pages.append(blocks)
    toc = []
    items_by_page = []
    destinations = {}
    numbered_sections = {}
    captions_by_kind = defaultdict(list)
    warnings = []
    body_chars = 0
    for index, (page, blocks) in enumerate(zip(document, pages)):
        filtered = []
        for block in blocks:
            rect, value = block["rect"], block["text"]
            if index > 0 and rect.y1 < page.rect.height * .1 and repeated_headers[value] > 1:
                continue
            if rect.y0 > page.rect.height * .9 and re.fullmatch(r"\d+", value.strip()):
                continue
            filtered.append(block)
        contents_page = sum(bool(re.search(r"(?:\.\s){10}", b["text"])) for b in filtered) > 3
        columns = _column_count(filtered, page)
        if source_hash in _VERIFIED_COLUMNS:
            columns, single_from = _VERIFIED_COLUMNS[source_hash]
            if single_from is not None and index >= single_from:
                columns = 1
        regions = _display_regions(page, filtered, columns, verified_regions)
        items = []
        for block in filtered:
            # Work at line granularity: a caption and nearby prose may share a
            # PDF text block, and never justify dropping the entire block.
            remaining = [line for line in block["lines"] if not any((_rect(line["bbox"]) & r["rect"]).get_area() > _rect(line["bbox"]).get_area() * .6 for r in regions)]
            if not remaining:
                continue
            block = {**block, "lines": remaining, "rect": _union([_rect(l["bbox"]) for l in remaining]), "text": _join([_text(l) for l in remaining]), "kind": "text"}
            heading = _heading(block, toc_titles, contents_page)
            if heading:
                level, title = heading
                block.update(kind="heading", level=level, title=title, id=f"section-{len(toc) + 1}")
                toc.append({"id": block["id"], "title": title, "level": level})
                match = _NUMBERED.match(title)
                if match:
                    numbered_sections[match[1] + match[2]] = block["id"]
                if title in {"参考文献", "References"}:
                    destinations["__references__"] = block["id"]
            body_chars += len(block["text"])
            items.append(block)
        for region in regions:
            if region["kind"] in {"figure", "table", "algorithm"}:
                captions_by_kind[region["kind"]].append(region)
            items.append(region)
        expected_captions = [b for b in filtered if _CAPTION.match(b["text"])]
        for caption in expected_captions:
            match = _CAPTION.match(caption["text"])
            if not any(r.get("id") == f"{match[1].lower()}-{int(match[2])}" for r in regions):
                warnings.append(f"第 {index + 1} 页 {match[1]} {match[2]} 未找到独立图表边界")
        numbered_lines = []
        for original in filtered:
            for line in original["lines"]:
                text = _text(line)
                label = re.search(r"\((\d{1,3})\)\s*$", text)
                if label and (re.fullmatch(r"\(\d{1,3}\)", text) or _mostly_math(original)):
                    numbered_lines.append((int(label[1]), _rect(line["bbox"])))
        for number, label_rect in numbered_lines:
            candidates = [item for item in items if item["rect"].intersects(label_rect)]
            if candidates:
                target = min(candidates, key=lambda item: abs(item["rect"].y0 - label_rect.y0))
                target.setdefault("aliases", []).append(f"equation-{number}")
        ordered = _reading_order(items, page, columns)
        if index == 0 and not any(entry["title"] == "摘要" for entry in toc):
            abstract = next((i for i, item in enumerate(ordered) if item["kind"] == "text" and len(_CJK.findall(item["text"])) > 100), None)
            if abstract is not None:
                heading = {"kind": "heading", "id": "abstract", "title": "摘要", "level": 1}
                ordered.insert(abstract, heading)
                toc.insert(0, {key: heading[key] for key in ("id", "title", "level")})
        items_by_page.append(ordered)
    names = document.resolve_names()
    # Stable, semantic destinations survive translated-PDF pagination changes.
    for name, destination in names.items():
        match = re.match(r"(?:section|subsection|subsubsection)\.([A-Z\d.]+)$", name)
        if match and match[1] in numbered_sections:
            destinations[name] = numbered_sections[match[1]]
        elif name.startswith("equation."):
            number = name.rsplit(".", 1)[-1]
            if any(r.get("id") == f"equation-{number}" or f"equation-{number}" in r.get("aliases", []) for items in items_by_page for r in items):
                destinations[name] = f"equation-{number}"
        elif name.startswith("cite.") and "__references__" in destinations:
            destinations[name] = destinations["__references__"]
    for kind, captions in captions_by_kind.items():
        named = sorted([name for name in names if name.startswith(kind + ".caption.")], key=lambda name: int(name.rsplit(".", 1)[-1]))
        actual = sorted(captions, key=lambda c: c["number"])
        if len(named) == len(actual):
            for name, caption in zip(named, actual):
                destinations[name] = caption["id"]
    renderer = _Renderer(asset_dir, destinations)
    output = []
    counts = Counter()
    used_ids = set()
    for page, items in zip(document, items_by_page):
        output.append(f'<div class="paper-source-page" id="page-{page.number + 1}"><a href="paper.zh.pdf#page={page.number + 1}">原文第 {page.number + 1} 页</a></div>')
        links = page.get_links()
        for item in items:
            for alias in item.get("aliases", []):
                if alias not in used_ids and alias != item.get("id"):
                    output.append(f'<span id="{alias}" class="paper-anchor"></span>')
                    used_ids.add(alias)
            kind = item["kind"]
            if kind == "heading":
                level = min(6, item["level"] + 1)
                output.append(f'<h{level} id="{item["id"]}">{escape(item["title"])}</h{level}>')
            elif kind == "text":
                output.append(f'<p class="paper-paragraph">{renderer.paragraph(page, item["lines"], links)}</p>')
            else:
                counts[kind] += 1
                src = renderer.crop(page, item["visual"], kind)
                anchor = item.get("id")
                # Multi-line equations can share a number; IDs must remain unique.
                id_attr = f' id="{anchor}"' if anchor and anchor not in used_ids else ""
                used_ids.add(anchor)
                alt = item["text"][:800] or "论文图表"
                width = item["visual"].width
                caption = renderer.paragraph(page, item.get("caption_lines", []), links)
                if not caption and kind == "equation":
                    caption = f'公式（{item["number"]}）' if item.get("number") else ""
                output.append(f'<figure class="paper-{kind}"{id_attr}><a href="{src}" target="_blank" rel="noopener"><img src="{src}" alt="{escape(alt, quote=True)}" loading="lazy" style="width:{width / 9.5:.2f}em;max-width:100%"></a>' + (f'<figcaption class="paper-caption">{caption}</figcaption>' if caption else "") + '</figure>')
    stats = {"pages": len(document), "text_chars": body_chars, "sections": len(toc), "figures": counts["figure"], "tables": counts["table"], "algorithms": counts["algorithm"], "equations": counts["equation"], "images": counts["image"], "assets": renderer.asset_count, "inline_math": renderer.inline_count, "links": renderer.link_count, "warnings": warnings, "converter_version": CONVERTER_VERSION}
    document.close()
    return {"body_html": "\n".join(output), "toc": toc, "stats": stats}
