"""arXiv metadata parsing and rate-limited HTTP for ID lookup and full text."""
import errno
import http.client
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, time as day_time, timedelta, timezone
from email.utils import parsedate_to_datetime

from .selection import affiliation_companies

NS = {"a": "http://www.w3.org/2005/Atom", "ar": "http://arxiv.org/schemas/atom",
      "os": "http://a9.com/-/spec/opensearch/1.1/"}
ENDPOINT = "https://export.arxiv.org/api/query"
LOG = logging.getLogger(__name__)


# "日"级边界（日报日期、--since/--until 日期）统一按 UTC+8 解释，与日报归档日期一致。
DAY_ZONE = timezone(timedelta(hours=8))


def utcnow():
    return datetime.now(timezone.utc)


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must include timezone: " + value)
    return parsed.astimezone(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def date_boundary(value, end=False):
    if len(value) == 10:
        parsed = datetime.combine(date.fromisoformat(value), day_time.min, tzinfo=DAY_ZONE)
        return (parsed + timedelta(days=1) if end else parsed).astimezone(timezone.utc)
    return timestamp(value)


def arxiv_id(value):
    value = re.sub(r"^arxiv:\s*", "", value.strip(), flags=re.I)
    if "://" in value:
        url = urllib.parse.urlparse(value)
        if url.hostname not in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
            raise ValueError("Not an arXiv URL: " + value)
        value = re.sub(r"^/(abs|pdf|html)/", "", url.path)
    value = re.sub(r"\.pdf$", "", value)
    match = re.fullmatch(r"(\d{4}\.\d{4,5}|[a-zA-Z.-]+/\d{7})(?:v([1-9]\d*))?", value)
    if not match:
        raise ValueError("Invalid arXiv identifier: " + value)
    return match[1], int(match[2] or 1)


def parse_atom(payload, config):
    root = ET.fromstring(payload)
    if root.tag != "{" + NS["a"] + "}feed":
        raise ValueError("Response is not an Atom feed")
    papers = []
    for entry in root.findall("a:entry", NS):
        def field(path):
            return " ".join((entry.findtext(path, default="", namespaces=NS) or "").split())
        identifier = field("a:id")
        if "/api/errors" in identifier:
            raise ValueError("arXiv API error: " + field("a:summary"))
        identifier, version = arxiv_id(identifier)
        title, abstract = field("a:title"), field("a:summary")
        if not title or not abstract:
            raise ValueError("Missing title/abstract for " + identifier)
        published, updated = iso(timestamp(field("a:published"))), iso(timestamp(field("a:updated")))
        authors = []
        for author in entry.findall("a:author", NS):
            affiliations = [" ".join((a.text or "").split()) for a in author.findall("ar:affiliation", NS)]
            authors.append({"name": " ".join((author.findtext("a:name", "", NS)).split()),
                            "affiliations": [a for a in affiliations if a]})
        affiliations = sorted({a for author in authors for a in author["affiliations"]})
        companies, evidence = affiliation_companies(affiliations, config)
        papers.append({"arxiv_id": identifier, "version": version, "title": title,
                       "abstract": abstract, "published": published, "updated": updated,
                       "authors": authors, "affiliations": affiliations,
                       "categories": sorted({c.get("term") for c in entry.findall("a:category", NS) if c.get("term")}),
                       "doi": field("ar:doi"), "comment": field("ar:comment"),
                       "journal_ref": field("ar:journal_ref"),
                       "url": "https://arxiv.org/abs/" + identifier,
                       "pdf_url": "https://arxiv.org/pdf/" + identifier + "v" + str(version),
                       "origin": "arxiv", "companies": companies,
                       "company_evidence_kind": "arxiv_affiliation" if evidence else "unknown",
                       "company_evidence": evidence})
    total = root.findtext("os:totalResults", namespaces=NS)
    start = root.findtext("os:startIndex", namespaces=NS)
    return papers, int(total) if total is not None else None, int(start) if start is not None else None


def retry_after(value):
    try:
        return max(0, float(value))
    except (ValueError, TypeError):
        try:
            return max(0, (parsedate_to_datetime(value) - utcnow()).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0


def network_permission_denied(error):
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    return isinstance(reason, OSError) and reason.errno in {errno.EPERM, errno.EACCES}


class ArxivClient:
    def __init__(self, config, cache_dir, *, metadata=False):
        self.config = config
        self.settings = config["arxiv"]
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.metadata = metadata
        # Also leave a full interval after a previous CLI process releases the DB lock.
        self.last_request = time.monotonic()

    def request(self, url, accept="application/atom+xml"):
        settings = self.settings
        # Candidate metadata has a durable retry queue. Do not hold an entire
        # daily run in an exponential 30+60+120+240+480 second 429 loop.
        retries = min(settings["retries"], 1) if self.metadata else settings["retries"]
        for attempt in range(retries + 1):
            if self.last_request is not None:
                time.sleep(max(0, settings["request_interval_seconds"] - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            request = urllib.request.Request(url, headers={"User-Agent": settings["user_agent"],
                                                          "Accept": accept})
            try:
                with urllib.request.urlopen(request, timeout=settings["timeout_seconds"]) as response:
                    return response.read()
            except urllib.error.HTTPError as error:
                if self.metadata and error.code == 429:
                    LOG.warning("arXiv 元数据限流，保留候选并延后重试")
                    raise
                if error.code not in {408, 429, 500, 502, 503, 504} or attempt == retries:
                    raise
                delay = max((30.0 if error.code == 429 else 3.1) * 2 ** attempt,
                            retry_after((error.headers or {}).get("Retry-After")))
                if self.metadata and delay > 5:
                    raise  # Honor long Retry-After in the persistent queue.
                LOG.warning("arXiv HTTP %s; retry in %.1fs", error.code, delay)
                error.close()
                time.sleep(delay)
            except (urllib.error.URLError, TimeoutError, ConnectionError, PermissionError,
                    http.client.HTTPException) as error:
                if network_permission_denied(error):
                    LOG.error("arXiv 网络权限被阻止", exc_info=True)
                    raise RuntimeError("arXiv 请求被当前执行环境的网络权限阻止（EPERM/EACCES）；请在允许联网的环境运行。") from None
                if attempt == retries:
                    raise
                LOG.warning("arXiv 网络请求失败，%.1f 秒后重试", 3.1 * 2 ** attempt, exc_info=True)
                time.sleep(3.1 * 2 ** attempt)
        raise RuntimeError("Unreachable retry state")
