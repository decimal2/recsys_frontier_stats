"""Extract citable text blocks from arXiv HTML or every page of a PDF."""
import hashlib
import json
import logging
from pathlib import Path

from .arxiv import ArxivClient, arxiv_id, iso, utcnow
from .publishing import atomic_write

LOG = logging.getLogger(__name__)


def extract_html(payload):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(payload, "html.parser")
    article = soup.select_one("article.ltx_document, .ltx_page_main")
    if article is None:
        raise ValueError("Not an arXiv full-text HTML document")
    for node in article.select("script, style, nav, .ltx_page_header, .ltx_page_footer"):
        node.decompose()
    blocks, section = [], "Front matter"
    authors = article.select_one(".ltx_authors")
    if authors:
        blocks.append({"id": "b0001", "section": "Authors and affiliations", "anchor": "",
                       "text": " ".join(authors.get_text(" ", strip=True).split())})
    tags = {"h1", "h2", "h3", "h4", "p", "table", "li", "figcaption"}
    for node in article.find_all(list(tags)):
        if any(parent.name in tags for parent in node.parents if parent is not article):
            continue
        text = " ".join(node.get_text(" ", strip=True).split())
        if not text:
            continue
        if node.name in {"h1", "h2", "h3", "h4"}:
            section = text
        blocks.append({"id": f"b{len(blocks) + 1:04d}", "section": section,
                       "anchor": node.get("id", ""), "text": text})
    return blocks, {"format": "html", "coverage": "all_extracted_text",
                    "note": "保留正文、表格文字和图注；没有视觉读取图片。"}


def extract_pdf(payload):
    import fitz
    blocks, sparse_pages = [], []
    with fitz.open(stream=payload, filetype="pdf") as document:
        for page_index, page in enumerate(document):
            page_chars = 0
            for block in page.get_text("blocks", sort=True):
                if block[6] != 0:
                    continue
                text = " ".join(block[4].split())
                if text:
                    page_chars += len(text)
                    blocks.append({"id": f"p{page_index + 1:03d}b{len(blocks) + 1:04d}",
                                   "page": page_index + 1, "section": f"Page {page_index + 1}", "text": text})
            if page_chars < 40:
                sparse_pages.append(page_index + 1)
        page_count = len(document)
    return blocks, {"format": "pdf", "page_count": page_count, "sparse_pages": sparse_pages,
                    "coverage": "all_extracted_text", "note": "提取了所有页面的可读取文字；图像、扫描内容及复杂表格可能未完整识别。"}


def package_document(payload, identifier, version, source_url, file_type):
    blocks, details = extract_pdf(payload) if file_type == "pdf" else extract_html(payload)
    if sum(len(block["text"]) for block in blocks) < 1200:
        raise ValueError("全文可提取文字不足，可能仅摘要、扫描页或错误页面；不会生成全文 insight。")
    return {"arxiv_id": identifier, "version": version, "source_url": source_url,
            "fetched_at": iso(utcnow()), "sha256": hashlib.sha256(payload).hexdigest(),
            "content_chars": sum(len(b["text"]) for b in blocks), "blocks": blocks, **details}


class FullTextStore:
    def __init__(self, config, directory):
        self.config = config
        self.directory = Path(directory)
        self.network = ArxivClient(config, self.directory / "requests")

    def location(self, identifier, version):
        identifier, _ = arxiv_id(identifier)
        return self.directory / (identifier.replace("/", "_") + "v" + str(version))

    def import_file(self, identifier, version, path):
        identifier, _ = arxiv_id(identifier)
        file_type = "pdf" if path.suffix.lower() == ".pdf" else "html"
        payload = path.read_bytes()
        document = package_document(payload, identifier, version, str(path.resolve()), file_type)
        document["local_import"] = True
        document["note"] += " 本地文件与论文 ID 的对应关系由导入者指定。"
        self.save(document, payload, file_type)
        LOG.info("本地全文导入 arxiv_id=%sv%s format=%s blocks=%s characters=%s", identifier, version,
                 file_type, len(document["blocks"]), document["content_chars"])
        return document

    def save(self, document, payload, file_type):
        directory = self.location(document["arxiv_id"], document["version"])
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ("source." + file_type)).write_bytes(payload)
        atomic_write(directory / "document.json", json.dumps(document, ensure_ascii=False, indent=2))

    def get(self, paper):
        identifier, version = paper["arxiv_id"], paper["version"]
        cached = self.location(identifier, version) / "document.json"
        if cached.exists():
            document = json.loads(cached.read_text(encoding="utf-8"))
            if document["arxiv_id"] != identifier or document["version"] != version:
                raise ValueError("Full-text cache identity mismatch")
            LOG.info("使用全文缓存 arxiv_id=%sv%s", identifier, version)
            return document
        suffix = identifier + ("v" + str(version) if version else "")
        errors = []
        for file_type in ("html", "pdf"):
            url = f"https://arxiv.org/{file_type}/{suffix}"
            try:
                LOG.info("下载全文 arxiv_id=%s format=%s", suffix, file_type)
                payload = self.network.request(url, accept="text/html,application/pdf")
                document = package_document(payload, identifier, version, url, file_type)
                self.save(document, payload, file_type)
                LOG.info("全文提取完成 arxiv_id=%s format=%s blocks=%s characters=%s", suffix, file_type,
                         len(document["blocks"]), document["content_chars"])
                return document
            except (OSError, ValueError) as error:
                LOG.warning("全文获取或提取失败 arxiv_id=%s format=%s", suffix, file_type, exc_info=True)
                errors.append(f"{file_type}: {type(error).__name__}")
        raise RuntimeError("无法获取全文（" + "; ".join(errors) + "）；可使用 fulltext --file 导入本地 PDF。")
