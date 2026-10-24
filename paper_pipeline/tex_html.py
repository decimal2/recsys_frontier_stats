"""Render a translated arXiv TeX project as semantic HTML with native MathML.

LaTeXML digests the actual macros, environments and bibliography. Figures are
copied from the project; PDF figure assets become SVG. Prose, mathematics and
tables never pass through PDF extraction.
"""
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from xml.dom import minidom

from bs4 import BeautifulSoup
import fitz

LOG = logging.getLogger(__name__)
LTX = "http://dlmf.nist.gov/LaTeXML"
XML_ID = "{http://www.w3.org/XML/1998/namespace}id"

STYLES = """
.tex-paper{min-width:0;line-height:1.85;overflow-wrap:anywhere}
.tex-paper .ltx_title_document{display:none}
.tex-paper .ltx_authors{font-size:14px;color:var(--muted);margin-bottom:24px}
.tex-paper .ltx_creator{display:inline-block;margin-right:1em}
.tex-paper .ltx_title{display:block;line-height:1.5;margin:1.5em 0 .7em;text-transform:none;letter-spacing:0}
.tex-paper .ltx_title_abstract{font-size:22px;color:var(--ink)}
.tex-paper .ltx_p{margin:0 0 1em;text-align:justify}
.tex-paper .ltx_para{margin:0}
.tex-paper .ltx_section,.tex-paper .ltx_subsection,.tex-paper .ltx_subsubsection{margin-top:24px}
.tex-paper .ltx_title_paragraph{font-size:17px}
.tex-paper .ltx_font_bold{font-weight:bold}.tex-paper .ltx_font_italic{font-style:italic}
.tex-paper .ltx_font_smallcaps{font-variant:small-caps}.tex-paper .ltx_font_typewriter{font-family:monospace}
.tex-paper .ltx_font_sansserif{font-family:sans-serif}
.tex-paper .ltx_align_left{text-align:left}.tex-paper .ltx_align_right{text-align:right}
.tex-paper .ltx_align_center{text-align:center}.tex-paper .ltx_align_top{vertical-align:top}
.tex-paper .ltx_align_middle{vertical-align:middle}.tex-paper .ltx_align_bottom{vertical-align:bottom}
.tex-paper .ltx_graphics{max-width:100%;height:auto;background:white}
.tex-paper figure{margin:24px 0;overflow-x:auto;text-align:center}
.tex-paper .ltx_caption{display:block;text-align:left;font-size:14px;line-height:1.7;margin:10px 0}
.tex-paper .ltx_figure_panel{display:inline-block;max-width:100%;vertical-align:top}
.tex-paper .ltx_tabular{display:table;width:auto;max-width:none;border-collapse:collapse;margin:12px auto;font-size:14px}
.tex-paper .ltx_tabular td,.tex-paper .ltx_tabular th{padding:5px 9px;border:0;line-height:1.55}
.tex-paper .ltx_tabular .ltx_border_t{border-top:1px solid currentColor}
.tex-paper .ltx_tabular .ltx_border_b{border-bottom:1px solid currentColor}
.tex-paper .ltx_tabular .ltx_border_tt{border-top:2px solid currentColor}
.tex-paper .ltx_tabular .ltx_border_bb{border-bottom:2px solid currentColor}
.tex-paper .ltx_tabular .ltx_border_l{border-left:1px solid currentColor}
.tex-paper .ltx_tabular .ltx_border_r{border-right:1px solid currentColor}
.tex-paper .ltx_equation,.tex-paper .ltx_eqn_table{display:table;width:100%;max-width:none;border-collapse:collapse;margin:18px 0}
.tex-paper .ltx_eqn_cell{border:0;padding:3px 6px;font-size:1em}
.tex-paper .ltx_eqn_eqno{white-space:nowrap;width:1%;padding-left:20px}
.tex-paper .ltx_eqn_center_padleft,.tex-paper .ltx_eqn_center_padright{width:50%;padding:0}
.tex-paper .tex-equation-scroll{overflow-x:auto;max-width:100%;margin:12px 0}
.tex-paper math{font-size:1em;line-height:normal;font-family:math}
.tex-paper math[display="block"]{margin:.2em 0;white-space:nowrap}
.tex-paper .ltx_biblist{list-style:none;margin:0;padding:0;font-size:14px;line-height:1.7}
.tex-paper .ltx_bibitem{margin:12px 0;scroll-margin-top:24px}
.tex-paper .ltx_bibblock{display:block}
.tex-paper .ltx_note_content{display:block;font-size:13px;color:var(--muted)}
.tex-paper .ltx_note_mark{font-size:75%;vertical-align:super}
.tex-paper .ltx_TOC,.tex-paper .ltx_toc{display:none}
.tex-paper .ltx_framed,.tex-paper .ltx_listing{padding:14px;border:1px solid var(--line);border-radius:6px;overflow:auto}
.tex-paper .ltx_item{margin:8px 0}.tex-paper .ltx_itemize,.tex-paper .ltx_enumerate{padding-left:1.5em}
.tex-paper .ltx_tag{margin-right:.35em}
.tex-paper [id]{scroll-margin-top:24px}
"""


def _tools(latexml_root):
    env = os.environ.copy()
    root = Path(latexml_root or env.get("LATEXML_ROOT", "")) if latexml_root or env.get("LATEXML_ROOT") else None
    if root:
        scripts = root / "usr/bin"
        library = root / "usr/share/perl5"
        if not (scripts / "latexmlc").is_file():
            raise ValueError("LATEXML_ROOT 中没有 usr/bin/latexmlc：" + str(root))
        env["PERL5LIB"] = str(library) + (os.pathsep + env["PERL5LIB"] if env.get("PERL5LIB") else "")
        return ["perl", str(scripts / "latexmlc")], ["perl", str(scripts / "latexmlpost")], env
    if not shutil.which("latexmlc") or not shutil.which("latexmlpost"):
        raise RuntimeError("TeX 全文构建需要 LaTeXML；安装后加入 PATH，或设置 LATEXML_ROOT 为解压目录。")
    return ["latexmlc"], ["latexmlpost"], env


def _run(command, cwd, env, log_path, timeout):
    LOG.info("TeX HTML 构建日志：%s", log_path)
    with log_path.open("w", encoding="utf-8") as log:
        completed = subprocess.run(command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
                                   timeout=timeout, check=False)
    output = log_path.read_text(encoding="utf-8", errors="replace")
    failures = re.findall(r"^(?:Fatal|Error):.*$", output, re.MULTILINE)
    if completed.returncode not in (0, 1) or failures:
        detail = "\n".join(failures[:12]) or output[-1800:]
        raise RuntimeError(f"LaTeXML 构建失败（详见 {log_path}）：\n{detail}")
    return re.findall(r"^(?:Warning|Info:unexpected):.*$", output, re.MULTILINE)


def _figure_source(node, source_dir):
    names = node.get("candidates", "").split(",") + [node.get("graphic", "")]
    for name in names:
        if not name:
            continue
        path = (source_dir / name).resolve()
        if path.is_relative_to(source_dir) and path.is_file():
            return path
    raise ValueError("TeX 图资源不存在：" + node.get("graphic", ""))


def _figure_asset(node, source_dir, asset_dir):
    path = _figure_source(node, source_dir)
    options = node.get("options", "")
    checksum = hashlib.sha256(path.read_bytes() + options.encode()).hexdigest()[:16]
    if path.suffix.lower() == ".pdf":
        target = asset_dir / (checksum + ".svg")
        with fitz.open(path) as pdf:
            page_option = re.search(r"(?:^|,)page=(\d+)", options)
            page_number = int(page_option[1]) - 1 if page_option else 0
            if not 0 <= page_number < len(pdf):
                raise ValueError("图片 PDF 页码不存在：" + str(path))
            page = pdf[page_number]
            trim = re.search(r"(?:^|,)trim=([^,]+)", options)
            if trim and "clip=true" in options:
                values = re.findall(r"(-?[\d.]+)pt", trim[1])
                if len(values) != 4:
                    raise ValueError("未支持的 TeX 图形 trim：" + options)
                left, bottom, right, top = [float(v) * 72 / 72.27 for v in values]
                rect = page.rect
                crop = fitz.Rect(rect.x0 + left, rect.y0 + top, rect.x1 - right, rect.y1 - bottom)
                if crop.is_empty:
                    raise ValueError("TeX 图片裁切区域为空：" + str(path))
                # TeX trim uses the displayed orientation; PyMuPDF CropBox
                # expects the unrotated PDF coordinate system.
                page.set_cropbox(crop * page.derotation_matrix)
            svg = page.get_svg_image(text_as_path=True)
            if len(svg.encode("utf-8")) > 2 * 1024 * 1024:
                # Dense scatter plots can turn a small PDF into tens of MB of
                # SVG paths. Keep a sharp 216 dpi, lossless rendering instead.
                target = asset_dir / (checksum + ".png")
                page.get_pixmap(matrix=fitz.Matrix(3, 3), alpha=True).save(target)
                LOG.info("密集矢量图转为 216 dpi 无损 PNG：%s（SVG %.1f MiB）", path.name,
                         len(svg.encode("utf-8")) / 1024 ** 2)
            else:
                target.write_text(svg, encoding="utf-8")
    elif path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}:
        if "trim=" in options and "clip=true" in options:
            raise ValueError("位图 trim 尚未实现，不能静默丢弃：" + str(path))
        target = asset_dir / (checksum + path.suffix.lower())
        shutil.copy2(path, target)
    else:
        raise ValueError("未支持的 TeX 图像格式：" + str(path))
    return "assets/" + target.name


def _prepare_bindings(source_dir, build_dir):
    """Only class/package adapters; the original and translated TeX stay intact."""
    directory = build_dir / "bindings"
    directory.mkdir(exist_ok=True)
    base = "package LaTeXML::Package::Pool;\nuse strict; use warnings; use LaTeXML::Package;\n"
    if (source_dir / "icml2024.sty").exists():
        bibliography = "AssignValue('NO_BIBTEX' => 1, 'global');" if (source_dir / "main.bbl").exists() else ""
        (directory / "icml2024.sty.ltxml").write_text(base + r"""
RequirePackage('icml_support');
DefMacro('\\icmlaffiliation{}{}','\\icmladdress{#2}');
DefMacro('\\icmlcorrespondingauthor{}{}','\\icmladdress{Correspondence: #2}');
""" + bibliography + "\n1;\n", encoding="utf-8")
    if (source_dir / "googledeepmind.cls").exists():
        # These packages change PDF layout/encoding, not manuscript content.
        # Biblatex citations are mapped to LaTeXML's native author/year resolver;
        # all original citation keys and .bib records are retained.
        bindings = {
            "compat.ltxml": r"""DefMacro('\\pdftrailerid{}','');
DefMacro('\\nobibliography OptionalMatch:*','');""",
            "googledeepmind.cls.ltxml": r"""
LoadClass('OmniBus');
foreach my $package (qw(amsthm amsmath amsfonts amssymb url hyperref pifont xcolor textcomp graphicx colortbl booktabs enumitem tabularx caption authblk fancyhdr)) {
  RequirePackage($package);
}
DefMacro('\\footerfont','\\normalfont');
""",
            "CJKutf8.sty.ltxml": r"""
DefEnvironment('{CJK*}{}{}','#body');
DefEnvironment('{CJK}{}{}','#body');
""",
            "biblatex.sty.ltxml": r"""
RequirePackage('natbib');
Let('\\textcite','\\citet');
Let('\\parencite','\\citep');
DefMacro('\\addbibresource[]{}', sub { my ($g,$opts,$file)=@_; AssignValue('HTML_BIB_RESOURCE' => ToString($file), 'global'); return; });
DefMacro('\\printbibliography[]', sub { return Invocation(T_CS('\\bibliography'), Tokens(T_OTHER(LookupValue('HTML_BIB_RESOURCE')))); });
DefMacro('\\DeclareFieldFormat{}{}','');
DefMacro('\\DeclareFieldAlias{}{}','');
DefMacro('\\savebibmacro{}','');
DefMacro('\\renewbibmacro OptionalMatch:* {}{}','');
DefMacro('\\nameyeardelim','');
DefMacro('\\textcitedelim','');
""",
            "tcolorbox.sty.ltxml": r"""
DefMacro('\\tcbuselibrary{}','');
DefMacro('\\tcbset{}','');
DefPrimitive('\\newtcolorbox{}[]{}', sub {
  my ($stomach,$name,$count,$opts)=@_;
  die "Only one-argument manuscript case boxes are supported" unless ToString($count) eq '1';
  DefEnvironment('{'.ToString($name).'}{}', "<ltx:block class='ltx_framed'><ltx:p><ltx:text class='ltx_font_bold'>#1</ltx:text></ltx:p>#body</ltx:block>");
});
DefEnvironment('{tcolorbox} OptionalKeyVals',
 "<ltx:block class='ltx_framed'><ltx:p>#title</ltx:p><ltx:p>#before</ltx:p>#body</ltx:block>",
 properties => sub { return (title => GetKeyVal($_[1],'title'), before => GetKeyVal($_[1],'before upper')); });
""",
        }
        # The manuscript loads dummy-text packages but invokes no dummy text.
        # Do not let their newer expl3 dependency swallow the old converter.
        active = "\n".join(re.sub(r"(?<!\\)%[^\n]*", "", p.read_text(encoding="utf-8"))
                           for p in source_dir.rglob("*.tex"))
        for name in ("lipsum", "kantlipsum", "blindtext", "newunicodechar"):
            if re.search(r"\\" + name + r"\b", active):
                raise ValueError("未实现的排版宏实际用于正文，不能跳过：" + name)
            bindings[name + ".sty.ltxml"] = "# Loaded but unused in this manuscript."
        for name, code in bindings.items():
            (directory / name).write_text(base + code + "\n1;\n", encoding="utf-8")
    # Paper-specific adapters must survive refreshing the standard bindings.
    for override in (build_dir / "bindings-overrides").glob("*.ltxml"):
        shutil.copy2(override, directory / override.name)
    return directory


def _bibliography_inputs(xml, source_dir, build_dir):
    """Normalize BibTeX compatibility issues in build copies only.

    Some official sources contain handles such as knight_raider. Biber accepts
    those, while LaTeXML reads the field as TeX. Citation keys and displayed
    names are unchanged; URL fields and mathematical expressions stay intact.
    """
    files = []
    changed = False
    for bibliography in xml.findall(".//{" + LTX + "}bibliography"):
        for name in bibliography.get("files", "").split(","):
            if not name:
                continue
            path = source_dir / name
            if path.suffix != ".bib":
                path = path.with_suffix(".bib")
            path = path.resolve()
            if not path.is_relative_to(source_dir) or not path.is_file():
                raise ValueError("BibTeX 文件不在源码工程内：" + name)
            original = path.read_text(encoding="utf-8")
            def names(match):
                value = match[2]
                if "$" not in value:
                    value = re.sub(r"(?<!\\)_", r"\\_", value)
                return match[1] + value + match[3]
            normalized = re.sub(r"(?im)^(\s*(?:author|editor)\s*=\s*\{)([^\n]*?)(\}\s*,?\s*$)", names, original)
            # LaTeXML 0.8.8 otherwise emits only author/year for @dataset,
            # silently losing its title and URL. Misc preserves those fields.
            normalized = re.sub(r"(?im)^(\s*)@dataset(?=\s*[{(])", r"\1@misc", normalized)
            # BibTeX has `jul`, but not bare `July`; some arXiv sources
            # nevertheless use full month names as undefined string macros.
            months = ("January", "February", "March", "April", "May", "June", "July", "August",
                      "September", "October", "November", "December")
            for month in months:
                if not re.search(r"(?i)@string\s*\{\s*" + month + r"\s*=", original):
                    normalized = re.sub(r"(?i)(\bmonth\s*=\s*)" + month + r"(?=\s*[,}])",
                                        lambda match: match[1] + "{" + month + "}", normalized)
            files.append((path.name, normalized))
            changed = changed or normalized != original
    if not changed:
        return []
    directory = build_dir / "bibliography"
    directory.mkdir(exist_ok=True)
    arguments = []
    for index, (name, content) in enumerate(files):
        target = directory / (str(index) + "-" + name)
        target.write_text(content, encoding="utf-8")
        arguments.append("--bibliography=" + str(target))
    LOG.info("BibTeX 兼容格式已在构建副本中规范化，原始引用键和源码保持不变：%s", directory)
    return arguments


def _normalize_subfigure_wrappers(xml_path):
    """Unwrap empty layout blocks emitted around subfig's figure children.

    LaTeXML 0.8.8 emits block > figure, which its own schema rejects. The
    enclosing figure accepts those subfigures directly. Keep all content,
    attributes, identifiers, whitespace and processing instructions intact.
    """
    document = minidom.parse(str(xml_path))
    changed = 0
    for block in list(document.getElementsByTagNameNS(LTX, "block")):
        elements = [node for node in block.childNodes if node.nodeType == node.ELEMENT_NODE]
        if (block.attributes.length or not elements
                or any(node.namespaceURI != LTX or node.localName != "figure" for node in elements)
                or any(node.nodeType == node.TEXT_NODE and node.data.strip() for node in block.childNodes)
                or block.parentNode.namespaceURI != LTX or block.parentNode.localName != "figure"):
            continue
        parent = block.parentNode
        for node in list(block.childNodes):
            parent.insertBefore(node, block)
        parent.removeChild(block)
        changed += 1
    if changed:
        xml_path.write_bytes(document.toxml(encoding="utf-8"))
        LOG.info("规范化 %s 个子图布局容器，保留所有图形、标题与引用", changed)
    return changed


def _normalize_html_tables(article):
    """Recover semantic tables when adjustbox emits inline span equivalents."""
    tables = article.select("span.ltx_tabular")
    names = {"ltx_tr": "tr", "ltx_td": "td", "ltx_th": "th", "ltx_thead": "thead",
             "ltx_tbody": "tbody", "ltx_tfoot": "tfoot", "ltx_tabular": "table"}
    for table in tables:
        for node in [table, *table.find_all(True)]:
            for class_name, name in names.items():
                if class_name in node.get("class", []):
                    node.name = name
                    break
            if node.name in {"td", "th"}:
                for class_name in node.get("class", []):
                    match = re.fullmatch(r"ltx_(rowspan|colspan)_(\d+)", class_name)
                    if match:
                        node[match[1]] = match[2]
        for parent in table.parents:
            if parent.name in {"td", "th", "figure", "article", "section"}:
                break
            if parent.name in {"span", "p"}:
                parent.name = "div"
    if tables:
        LOG.info("恢复 %s 个内联表格为 HTML table/tr/td，保留原始数据和合并单元格", len(tables))
    return len(tables)


def convert_tex(source_dir, entrypoint, asset_dir, build_dir, latexml_root=None, timeout=600):
    """Return body_html, toc, stats, styles; source_dir is a full translated project."""
    source_dir = Path(source_dir).resolve()
    entry = (source_dir / entrypoint).resolve()
    if not entry.is_relative_to(source_dir) or not entry.is_file():
        raise ValueError("TeX 入口必须是工程内现有文件：" + str(entrypoint))
    build_dir = Path(build_dir).resolve()
    asset_dir = Path(asset_dir).resolve()
    build_dir.mkdir(parents=True, exist_ok=True)
    asset_dir.mkdir(parents=True, exist_ok=True)
    digest, post, env = _tools(latexml_root)
    bindings = _prepare_bindings(source_dir, build_dir)
    preloads = ["--preload=compat.ltxml"] if (bindings / "compat.ltxml").exists() else []
    xml_path, html_path = build_dir / "document.xml", build_dir / "document.html"
    for path in (xml_path, html_path):
        path.unlink(missing_ok=True)
    warnings = _run(digest + preloads + ["--format=xml", "--nopost", "--path=" + str(bindings),
                    "--timeout=" + str(timeout), "--log=" + str(build_dir / "latexml.log"),
                    "--destination=" + str(xml_path), str(entry)],
                    source_dir, env, build_dir / "digest.log", timeout + 30)
    if not xml_path.is_file():
        raise RuntimeError("LaTeXML 未生成 XML；详见 " + str(build_dir / "digest.log"))
    _normalize_subfigure_wrappers(xml_path)
    xml = ET.parse(xml_path).getroot()
    xml_errors = xml.findall(".//{" + LTX + "}ERROR")
    if xml_errors:
        raise RuntimeError("LaTeXML XML 仍有错误节点：" + " ".join(''.join(x.itertext()) for x in xml_errors[:8]))
    graphics = {}
    for graphic in xml.findall(".//{" + LTX + "}graphics"):
        if not graphic.get(XML_ID):
            raise ValueError("源码图形缺少可追踪的 XML ID")
        graphics[graphic.get(XML_ID)] = _figure_asset(graphic, source_dir, asset_dir)
    warnings += _run(post + _bibliography_inputs(xml, source_dir, build_dir) +
                    ["--format=html5", "--pmml", "--mathtex", "--nomathimages", "--nographicimages",
                    "--path=" + str(bindings), "--log=" + str(build_dir / "latexmlpost.log"),
                    "--nodefaultresources", "--sourcedirectory=" + str(source_dir),
                    "--destination=" + str(html_path), str(xml_path)],
                    source_dir, env, build_dir / "post.log", timeout + 30)
    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
    article = soup.select_one("article.ltx_document")
    if article is None or article.select_one(".ltx_ERROR, merror"):
        raise RuntimeError("LaTeXML HTML 缺少正文或仍包含错误节点")
    _normalize_html_tables(article)
    article["class"] = article.get("class", []) + ["tex-paper"]
    for identifier, src in graphics.items():
        image = article.find("img", id=identifier)
        if image is None:
            raise RuntimeError("HTML 丢失源码图形：" + identifier)
        image["src"] = src
        image["loading"] = "lazy"
        image["class"] = [name for name in image.get("class", []) if not name.startswith("ltx_missing")]
        link = soup.new_tag("a", href=src, target="_blank", rel="noopener")
        image.wrap(link)
    for image in article.find_all("img"):
        if not image.get("src"):
            raise RuntimeError("HTML 存在未解析图片：" + str(image.get("id")))
    for table in article.select("table.ltx_equation,table.ltx_eqn_table"):
        if table.find_parent("div", class_="tex-equation-scroll") is None:
            wrapper = soup.new_tag("div", attrs={"class": "tex-equation-scroll"})
            table.wrap(wrapper)
    toc = []
    for title in article.select(".ltx_title_abstract,.ltx_title_section,.ltx_title_subsection,.ltx_title_subsubsection,.ltx_title_bibliography,.ltx_title_appendix"):
        parent = title.parent
        identifier = parent.get("id") or "tex-section-" + str(len(toc) + 1)
        parent["id"] = identifier
        kind = title.get("class", [])
        if "ltx_title_abstract" in kind and title.get_text(strip=True).rstrip(".") == "Abstract":
            title.string = "摘要"
        if "ltx_title_bibliography" in kind and title.get_text(strip=True) == "References":
            title.string = "参考文献"
        level = 3 if "ltx_title_subsubsection" in kind else 2 if "ltx_title_subsection" in kind else 1
        toc.append({"id": identifier, "title": title.get_text(" ", strip=True), "level": level})
    identifiers = {node["id"] for node in article.select("[id]")}
    bad_links = [a.get("href") for a in article.select('a[href^="#"]') if a.get("href")[1:] not in identifiers]
    if bad_links:
        raise RuntimeError("TeX HTML 有未解析引用：" + ", ".join(bad_links[:12]))
    unresolved = article.select(".ltx_ref[title='?'],.ltx_missing,.ltx_missing_label,.ltx_missing_citation")
    if unresolved:
        detail = ", ".join(node.get_text(" ", strip=True) or node.get("id", "") for node in unresolved[:12])
        raise RuntimeError("TeX HTML 含有未解析的引用或资源：" + detail)
    figures = [node for node in article.select("figure.ltx_figure") if not node.find_parent("figure", class_="ltx_figure")]
    stats = {"format": "arxiv_tex", "converter": "LaTeXML", "text_chars": len(article.get_text()),
             "sections": len(toc), "figures": len(figures),
             "tables": len(article.select("figure.ltx_table")), "table_cells": len(article.select(".ltx_tabular td,.ltx_tabular th")),
             "mathml": len(article.find_all("math")), "graphics": len(graphics),
             "references": len(article.select(".ltx_bibitem")), "warnings": warnings}
    result = {"format": "arxiv_tex", "body_html": str(article), "toc": toc, "stats": stats,
              "styles": STYLES, "extra_head": "<style>" + STYLES + "</style>"}
    (build_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
