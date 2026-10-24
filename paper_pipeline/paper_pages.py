"""Build a local Chinese full-paper library independently of the daily agent."""
import hashlib
import json
import logging
import re
import shutil
import tempfile
from pathlib import Path

from .publishing import atomic_write, escape, page, publish_index, read_paper_catalog, render_daily

ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger(__name__)

READING_STYLES = """
.paper-reader main,.paper-reader>header{max-width:1180px}
.paper-original-title{font-size:16px;color:var(--muted);margin:12px 0;overflow-wrap:anywhere}
.paper-actions{display:flex;flex-wrap:wrap;gap:12px;margin:20px 0}
.paper-actions a{padding:8px 14px;border:1px solid var(--line);border-radius:7px;background:var(--surface);font-size:14px}
.paper-actions a:first-child{background:var(--accent);color:var(--bg);border-color:var(--accent)}
.paper-reading-layout{display:grid;grid-template-columns:216px minmax(0,1fr);gap:36px;align-items:start;margin-top:30px}
.paper-toc{position:sticky;top:20px;max-height:calc(100vh - 40px);overflow:auto;border-right:1px solid var(--line);padding-right:14px;font-size:13px}
.paper-toc summary{font-size:14px;font-weight:600;cursor:pointer;padding:4px 0 10px}
.paper-toc ol{list-style:none;padding:0;margin:0}
.paper-toc li{margin:0;padding:4px 0;line-height:1.5}
.paper-toc a{color:var(--muted);display:block;padding:2px 4px;border-radius:4px}
.paper-toc a:hover{color:var(--accent);background:var(--accent-soft);text-decoration:none}
.paper-toc .toc-level-2{padding-left:12px}.paper-toc .toc-level-3{padding-left:24px}
.paper-fulltext{min-width:0;font-size:17px;line-height:1.9;overflow-wrap:anywhere}
.paper-fulltext p{margin:0 0 1em;text-align:justify}
.paper-fulltext h2,.paper-fulltext h3,.paper-fulltext h4,.paper-fulltext h5{display:block;color:var(--ink);text-transform:none;letter-spacing:0;line-height:1.5;margin:1.7em 0 .8em;scroll-margin-top:20px}
.paper-fulltext h2{font-size:24px}.paper-fulltext h3{font-size:21px}.paper-fulltext h4{font-size:18px}
.paper-fulltext [id]{scroll-margin-top:24px}
.paper-fulltext :target{outline:2px solid var(--accent);outline-offset:5px}
.paper-fulltext figure{margin:24px 0;overflow-x:auto;text-align:center}
.paper-fulltext figure img{max-width:100%;height:auto;background:white;vertical-align:middle}
.paper-fulltext figure a{display:block}
.paper-fulltext .paper-caption,.paper-fulltext figcaption{font-size:14px;line-height:1.7;text-align:left;color:var(--muted);margin:8px 0 16px}
.paper-fulltext .paper-equation{padding:8px 0}
.paper-fulltext .paper-equation img{max-height:240px;width:auto;max-width:100%}
.paper-fulltext .paper-inline-math{display:inline-block;vertical-align:middle;max-height:2.3em;width:auto;background:white;max-width:100%}
.paper-fulltext .paper-reference{font-size:14px;line-height:1.75;overflow-wrap:anywhere}
.paper-fulltext .paper-source-page{display:block;color:var(--muted);font-size:11px;border-top:1px solid var(--line);margin:32px 0 18px;padding-top:8px;text-align:right}
.paper-fulltext table{display:block;max-width:100%;overflow:auto;border-collapse:collapse;font-size:14px}
.paper-fulltext td,.paper-fulltext th{padding:6px 10px;border:1px solid var(--line)}
.paper-bottom{margin-top:40px;border-top:1px solid var(--line);padding-top:18px;display:flex;flex-wrap:wrap;gap:20px;font-size:14px}
.paper-library{display:grid;gap:0;margin:24px 0}
.paper-entry{padding:26px 0;border-top:1px solid var(--line)}
.paper-entry h2{display:block;font-size:21px;margin:6px 0}
.paper-entry .original{display:block;margin-bottom:12px}
.paper-entry .paper-actions{margin-bottom:0}
.paper-library-filters{display:flex;gap:12px;align-items:start;margin:24px 0 0}
.paper-library-filters input{margin:0;flex:1;min-width:0}
.paper-library-filters select{font:inherit;color:var(--ink);background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:12px;font-size:14px;max-width:40%}
@media(max-width:800px){
 .paper-reading-layout{display:block;margin-top:20px}
 .paper-toc{position:static;max-height:none;overflow:visible;border:1px solid var(--line);padding:10px 14px;margin-bottom:24px;border-radius:8px}
 .paper-toc[open] ol{max-height:55vh;overflow:auto}
 .paper-fulltext{font-size:16px;line-height:1.9}
 .paper-fulltext h2{font-size:22px}.paper-fulltext h3{font-size:19px}
 .paper-reader h1{font-size:26px}
}
@media print{.paper-toc,.paper-actions,.site-nav,.paper-bottom{display:none}.paper-reading-layout{display:block}.paper-fulltext{font-size:11pt}}
"""


def render_paper(paper, document):
    toc = ''.join(
        f'<li class="toc-level-{min(3, max(1, item["level"]))}"><a href="#{escape(item["id"])}">{escape(item["title"])}</a></li>'
        for item in document["toc"])
    tex_source = document.get("format") == "arxiv_tex"
    metadata = [*paper.get("companies", []), "arXiv " + paper["arxiv_id"], "译文对应 " + paper["version"]]
    if not tex_source and document["stats"].get("pages"):
        metadata.append(f'{document["stats"]["pages"]} 页中文原稿')
    labels = " · ".join(metadata)
    reading_note = "中文全文 · 章节、公式和参考文献支持直接跳转" if tex_source else "中文全文 · 公式与图表可点击查看大图"
    body = f'''<p class="paper-original-title">{escape(paper['title_en'])}</p>
<p class="muted">{escape(labels)}</p>
<div class="paper-actions"><a href="{escape(paper['arxiv_url'])}" target="_blank" rel="noopener">英文 arXiv ↗</a>
<a href="../index.html">全部论文</a></div>
<p class="muted">{reading_note}</p>
<div class="paper-reading-layout"><details class="paper-toc" open><summary>章节目录</summary>
<ol>{toc}</ol></details><article class="paper-fulltext" aria-label="论文全文">{document['body_html']}</article></div>
<div class="paper-bottom"><a href="../index.html">← 返回论文列表</a><a href="#">回到页首 ↑</a></div>
<script>
const tocPanel=document.querySelector('.paper-toc');
if(window.matchMedia('(max-width:800px)').matches) tocPanel.open=false;
tocPanel.addEventListener('click',e=>{{if(e.target.closest('a')&&window.matchMedia('(max-width:800px)').matches) tocPanel.open=false;}});
</script>'''
    return page(paper["title_zh"], body, "PAPER LIBRARY / " + paper["short_name"],
                root_prefix="../../", section="papers", body_class="paper-reader",
                extra_head=f"<style>{READING_STYLES}</style>" + document.get("extra_head", ""))


def render_library(papers):
    entries = []
    for paper in papers:
        topics = paper.get("topics", [])
        keywords = " ".join([paper["short_name"], paper["title_zh"], paper["title_en"], paper["arxiv_id"],
                             *paper.get("companies", []), *topics])
        pills = ' '.join(f'<span class="pill">{escape(tag)}</span>' for tag in topics)
        entries.append(f'''<article class="paper-entry" data-keywords="{escape(keywords)}" data-topics="{escape(json.dumps(topics, ensure_ascii=False))}">
<span class="eyebrow">{escape(paper['short_name'])}</span><h2><a href="{escape(paper['arxiv_id'])}/index.html">{escape(paper['title_zh'])}</a></h2>
<span class="original">{escape(paper['title_en'])}</span><p>{pills}</p>
<p class="muted">{escape(' · '.join(paper.get('companies', [])))} · {escape(paper['arxiv_id'] + paper['version'])}</p>
<div class="paper-actions"><a href="{escape(paper['arxiv_id'])}/index.html">阅读中文全文</a>
<a href="{escape(paper['arxiv_url'])}" target="_blank" rel="noopener">英文 arXiv ↗</a></div></article>''')
    topics = sorted({tag for paper in papers for tag in paper.get("topics", [])})
    options = ''.join(f'<option value="{escape(tag)}">{escape(tag)}</option>' for tag in topics)
    body = f'''<p class="lede">深入阅读推荐系统论文，保留中文全文、公式、图表与参考文献。</p>
<div class="paper-library-filters"><input id="paper-search" type="search" placeholder="搜索论文标题、简称、公司或 arXiv 编号…" aria-label="搜索论文">
<select id="paper-topic" aria-label="按主题筛选"><option value="">全部主题</option>{options}</select></div>
<p id="paper-count" class="muted" aria-live="polite">共 {len(papers)} 篇中文全文</p>
<div class="paper-library">{''.join(entries)}</div><p id="paper-empty" class="muted" hidden>没有匹配的论文。</p>
<script>
const paperSearch=document.querySelector('#paper-search'),paperTopic=document.querySelector('#paper-topic');
function filterPapers(){{let count=0;const query=paperSearch.value.toLowerCase().trim();
 document.querySelectorAll('.paper-entry').forEach(el=>{{
  el.hidden=!el.dataset.keywords.toLowerCase().includes(query)||(paperTopic.value&&!JSON.parse(el.dataset.topics).includes(paperTopic.value));
  if(!el.hidden)count++;
 }});document.querySelector('#paper-count').textContent='共 '+count+' 篇中文全文';document.querySelector('#paper-empty').hidden=count!==0;
}}
paperSearch.addEventListener('input',filterPapers);paperTopic.addEventListener('change',filterPapers);
</script>'''
    return page("论文阅读", body, "PAPER LIBRARY / 全文阅读", root_prefix="../", section="papers",
                extra_head=f"<style>{READING_STYLES}</style>")


def load_catalog(path):
    papers = json.loads(Path(path).read_text(encoding="utf-8"))["papers"]
    seen = set()
    for paper in papers:
        identifier = paper["arxiv_id"]
        if not re.fullmatch(r"\d{4}\.\d{4,5}", identifier) or identifier in seen:
            raise ValueError("论文目录中的 arXiv ID 重复或无效：" + identifier)
        if not re.fullmatch(r"v[1-9]\d*", paper["version"]):
            raise ValueError("论文目录需要明确 arXiv 版本：" + identifier)
        source_keys = ("tex_project", "tex_entry") if paper.get("tex_project") else ("zh_pdf",)
        for key in ("title_zh", "title_en", "short_name", *source_keys):
            if not isinstance(paper[key], str) or not paper[key].strip():
                raise ValueError("论文目录缺少 " + key)
        paper["arxiv_url"] = "https://arxiv.org/abs/" + identifier + paper["version"]
        seen.add(identifier)
    return papers


def project_checksum(directory):
    """Hash source and resource bytes so an edited include or figure rebuilds HTML."""
    directory = Path(directory)
    if not directory.is_dir():
        raise ValueError("找不到中文 TeX 工程：" + str(directory))
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError("TeX 工程中不支持符号链接：" + str(path))
        if path.is_file():
            digest.update(str(path.relative_to(directory)).encode())
            digest.update(b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def build_papers(source_dir, output_dir, catalog_path=ROOT / "paper_catalog.json", identifiers=None, force=False):
    from .paper_html import convert_pdf

    source_dir = Path(source_dir).resolve()
    output_dir = Path(output_dir)
    site = output_dir / "site"
    library = site / "papers"
    cache_dir = output_dir / "paper_cache"
    library.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    papers = load_catalog(catalog_path)
    if identifiers:
        wanted = {value.casefold() for value in identifiers}
        known = {str(paper[key]).casefold() for paper in papers for key in ("short_name", "arxiv_id")}
        if wanted - known:
            raise ValueError("论文目录中没有：" + ", ".join(sorted(wanted - known)))
        papers = [paper for paper in papers if wanted & {paper["short_name"].casefold(), paper["arxiv_id"]}]
    published = {paper["arxiv_id"]: paper for paper in read_paper_catalog(site)}
    built, cached, skipped = [], [], []
    for paper in papers:
        identifier = paper["arxiv_id"]
        tex_project = Path(paper["tex_project"]) if paper.get("tex_project") else None
        pdf_path = (source_dir / paper["zh_pdf"]).resolve() if paper.get("zh_pdf") else None
        if pdf_path and not pdf_path.is_relative_to(source_dir):
            raise ValueError("中文 PDF 必须位于 source-dir 内：" + paper["zh_pdf"])
        if not tex_project and (not pdf_path or not pdf_path.is_file()):
            LOG.warning("跳过 %s：没有中文 PDF（%s）", paper["short_name"], pdf_path)
            skipped.append(identifier)
            continue
        if tex_project:
            checksum = hashlib.sha256((project_checksum(tex_project) + "\0" + paper["tex_entry"]).encode()).hexdigest()
        else:
            checksum = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        converter_file = "tex_html.py" if tex_project else "paper_html.py"
        converter_hash = hashlib.sha256(Path(__file__).with_name(converter_file).read_bytes()).hexdigest()
        destination = library / identifier
        assets = destination / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / (identifier + ".json")
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
        except ValueError:
            LOG.warning("正文缓存损坏，将重新转换：%s", identifier, exc_info=True)
            cache = {}
        current = (not force and cache.get("source_sha256") == checksum
                   and cache.get("converter_sha256") == converter_hash
                   and all((assets / name).is_file() for name in cache.get("assets", []))
                   and "document" in cache)
        if current:
            document = cache["document"]
            cached.append(identifier)
            LOG.info("复用中文正文缓存：%s", paper["short_name"])
        else:
            LOG.info("转换中文全文：%s (%s)", paper["short_name"], tex_project or pdf_path.name)
            with tempfile.TemporaryDirectory(prefix=identifier + "-", dir=cache_dir) as temporary:
                staged = Path(temporary) / "assets"
                staged.mkdir()
                if tex_project:
                    from .tex_html import convert_tex
                    document = convert_tex(tex_project, paper["tex_entry"], staged,
                                           cache_dir / (identifier + "-tex-build"), paper.get("latexml_root"))
                else:
                    document = convert_pdf(pdf_path, staged)
                current_assets = {path.name for path in staged.iterdir() if path.is_file()}
                for path in staged.iterdir():
                    if path.is_file():
                        shutil.copy2(path, assets / path.name)
                for path in assets.iterdir():
                    if path.is_file() and path.name not in current_assets:
                        path.unlink()
            atomic_write(cache_path, json.dumps({"source_sha256": checksum, "converter_sha256": converter_hash,
                         "assets": sorted(path.name for path in assets.iterdir() if path.is_file()),
                         "document": document}, ensure_ascii=False))
            built.append(identifier)
        atomic_write(destination / "index.html", render_paper(paper, document))
        for legacy_pdf in ("paper.zh.pdf", "paper_zh.pdf"):
            (destination / legacy_pdf).unlink(missing_ok=True)
        for warning in document["stats"].get("warnings", []):
            LOG.warning("%s 全文转换提示：%s", paper["short_name"], warning)
        public = {key: value for key, value in paper.items() if key not in {"zh_pdf", "tex_project", "tex_entry", "latexml_root"}}
        public.update(url=f"papers/{identifier}/index.html", stats=document["stats"], source_sha256=checksum)
        published[identifier] = public
        LOG.info("中文论文页面已生成：%s；%s", destination / "index.html", document["stats"])
    catalog = list(published.values())
    atomic_write(library / "catalog.json", json.dumps({"papers": catalog}, ensure_ascii=False, indent=2))
    atomic_write(library / "index.html", render_library(catalog))
    paper_links = {paper["arxiv_id"]: paper["url"] for paper in catalog}
    for saved in (output_dir / "daily").glob("????-??-??.json"):
        payload = json.loads(saved.read_text(encoding="utf-8"))
        _, webpage = render_daily(payload, paper_links=paper_links)
        atomic_write(site / (saved.stem + ".html"), webpage)
    homepage = publish_index(None, site)
    return {"status": "completed" if catalog else "empty", "built": built, "cached": cached, "skipped": skipped,
            "papers": len(catalog), "library": str(library / "index.html"), "homepage": homepage}
