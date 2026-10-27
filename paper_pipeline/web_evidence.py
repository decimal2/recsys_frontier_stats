"""Recognize explicit unusable pages and remember failed URLs for a bounded time."""
import html
import json
import logging
import re
import string
import time

from .providers import redact_secrets
from .publishing import atomic_write

LOG = logging.getLogger(__name__)
TRANSIENT_TTL = 300
PAGE_TTL = 6 * 60 * 60
MAX_FAILURES = 512


def page_problem(content):
    text = re.sub(r"\\([" + re.escape(string.punctuation) + r"])", r"\1", html.unescape(content)).lower()
    if "本期为订阅内容" in text or "subscribe to read the full article" in text:
        return "subscription_preview", "这是订阅预览，只能作为线索；请用标题搜索论文原文或公司公开论文页确认。"
    if "please correct errors in the form below" in text and "whoops!" in text:
        return "search_form_error", "这是搜索表单错误页，没有论文结果；请修改查询或改用论文直链。"
    if "no updates for this time period" in text:
        return "empty_listing", "这是空列表；请停止继续翻此列表，改换公司、主题或来源搜索。"
    return None


class FailedPages:
    """Exact URL memory only; an unavailable page never bans its entire domain."""
    def __init__(self, path):
        self.path = path

    def load(self):
        try:
            records = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(records, dict):
                raise ValueError("Expected a URL-to-failure object")
            now = time.time()
            return {url: item for url, item in records.items()
                    if isinstance(item, dict) and isinstance(item.get("expires_at"), (int, float))
                    and item["expires_at"] > now and isinstance(item.get("result"), dict)}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            LOG.warning("无法读取网页失败缓存，将重新尝试来源 path=%s", self.path, exc_info=True)
            return {}

    def get(self, url):
        item = self.load().get(url)
        if item:
            return {**item["result"], "cached": True, "retry_after_seconds": max(1, int(item["expires_at"] - time.time()))}
        return None

    def remember(self, url, result, ttl):
        records = self.load()
        # Preserve status/action, not fetched content or credentials.
        result = {key: value for key, value in result.items() if key not in {"content", "title", "truncated"}}
        records.pop(url, None)
        records[url] = {"expires_at": time.time() + ttl, "result": result}
        records = dict(list(records.items())[-MAX_FAILURES:])
        try:
            atomic_write(self.path, redact_secrets(json.dumps(records, ensure_ascii=False)))
        except OSError:
            LOG.warning("无法保存网页失败缓存 path=%s", self.path, exc_info=True)

    def hints(self):
        records = self.load()
        return [{"url": url, "status": item["result"].get("page_status", "fetch_failed"),
                 "http_status": item["result"].get("http_status"),
                 "retry_after_seconds": max(1, int(item["expires_at"] - time.time()))}
                for url, item in list(records.items())[-20:]]
