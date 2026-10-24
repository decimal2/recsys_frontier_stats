"""Validate JSON structure only; insights summarize papers, they don't audit them."""
import hashlib
import json
import logging
from pathlib import Path

from .arxiv import iso, utcnow
from .publishing import atomic_write

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
COMPANY_RULE = (PROMPTS / "company_rule.txt").read_text(encoding="utf-8").strip()
LOG = logging.getLogger(__name__)


def require_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Missing text: " + label)


def require_object(value, label):
    if not isinstance(value, dict):
        raise ValueError("Expected object: " + label)


def require_strings(value, label):
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("Expected array of strings: " + label)


def require_string(value, label):
    if not isinstance(value, str):
        raise ValueError("Expected string: " + label)


def validate_analysis(result):
    require_object(result, "analysis")
    for key in ("title_zh", "one_liner", "relevance_reason", "recommendation_reason", "problem", "method"):
        require_text(result.get(key), key)
    if type(result.get("in_scope")) is not bool or type(result.get("rating")) is not int or not 1 <= result["rating"] <= 5:
        raise ValueError("Invalid in_scope/rating")
    for key in ("method_evidence_ids", "key_results", "insights", "limitations", "affiliations", "evidence"):
        if not isinstance(result.get(key), list):
            raise ValueError("Expected array: " + key)
    require_strings(result["method_evidence_ids"], "method_evidence_ids")
    for item in result["evidence"]:
        require_object(item, "evidence")
        for key in ("id", "block_id", "quote"):
            require_string(item.get(key), "evidence." + key)
    for finding in result["key_results"]:
        require_object(finding, "key_results")
        require_text(finding.get("finding"), "finding")
        require_string(finding.get("evidence_id"), "finding.evidence_id")
    for insight in result["insights"]:
        require_object(insight, "insight")
        require_text(insight.get("idea"), "insight.idea")
        if type(insight.get("is_inference")) is not bool:
            raise ValueError("Expected boolean: insight.is_inference")
        require_strings(insight.get("evidence_ids"), "insight.evidence_ids")
    for limitation in result["limitations"]:
        require_text(limitation, "limitation")
    for affiliation in result["affiliations"]:
        require_object(affiliation, "affiliation")
        require_text(affiliation.get("organization"), "organization")
        require_string(affiliation.get("evidence_id"), "affiliation.evidence_id")
    return result


def analyze(paper, document, client, config, directory):
    if document["content_chars"] > config["digest"]["max_fulltext_chars"]:
        raise ValueError("全文超过 max_fulltext_chars 预算；未截断冒充全文，请调整配置后重试。")
    system = (PROMPTS / "paper_insight.txt").read_text(encoding="utf-8").replace("{company_rule}", COMPANY_RULE)
    user = json.dumps({"paper": paper, "preferred_companies": config["preferred_companies"],
                       "extraction_note": document["note"], "blocks": document["blocks"]}, ensure_ascii=False)
    signature = json.dumps({"system": system, "input": user, "source_hash": document["sha256"],
                            "provider": client.provider, "profile": client.profile,
                            "max_tokens": client.settings["max_tokens"]}, sort_keys=True, ensure_ascii=False)
    key = hashlib.sha256(signature.encode()).hexdigest()
    path = Path(directory) / (key + ".json")
    if path.exists():
        result = json.loads(path.read_text(encoding="utf-8"))
        validate_analysis(result["analysis"])
        LOG.info("使用全文 insight 缓存 arxiv_id=%s provider=%s", paper["arxiv_id"], client.provider)
        return result
    failure = None
    for attempt in range(2):
        correction = "" if not failure else "\n上次输出未通过校验：" + str(failure) + "。请返回修正后的完整 JSON。"
        try:
            result, invocation = client.json(system, user + correction)
            validate_analysis(result)
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            LOG.warning("全文 insight JSON 格式或结构校验失败 arxiv_id=%s attempt=%s/2", paper["arxiv_id"], attempt + 1, exc_info=True)
            failure = error
            continue
        artifact = {"analysis": result, "invocation": invocation, "source_sha256": document["sha256"],
                    "source_url": document["source_url"], "extraction": {k: v for k, v in document.items() if k != "blocks"},
                    "created_at": iso(utcnow()), "cache_key": key}
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps(artifact, ensure_ascii=False, indent=2))
        return artifact
    raise ValueError("全文解读未通过 JSON 格式或结构校验：" + str(failure))


def validate_synthesis(result):
    require_object(result, "daily synthesis")
    if not isinstance(result.get("trends"), list) or not isinstance(result.get("topic"), dict):
        raise ValueError("Invalid daily synthesis structure")
    for item in result["trends"] + [result["topic"]]:
        require_object(item, "synthesis item")
        require_text(item.get("title"), "synthesis title")
        require_text(item.get("analysis"), "synthesis analysis")
        require_strings(item.get("paper_ids"), "synthesis.paper_ids")


def synthesize(items, client, directory):
    if not items:
        return {"trends": [], "topic": None}
    system = (PROMPTS / "daily_synthesis.txt").read_text(encoding="utf-8")
    user = json.dumps([{"arxiv_id": item["paper"]["arxiv_id"], "analysis": item["insight"]["analysis"]} for item in items],
                      ensure_ascii=False, sort_keys=True)
    key = hashlib.sha256((system + user + json.dumps(client.profile, sort_keys=True) + client.provider).encode()).hexdigest()
    path = Path(directory) / ("daily-" + key + ".json")
    if path.exists():
        saved = json.loads(path.read_text(encoding="utf-8"))
        validate_synthesis(saved)
        LOG.info("使用日报综合缓存 papers=%s provider=%s", len(items), client.provider)
        return saved
    failure = None
    for attempt in range(2):
        try:
            result, _ = client.json(system, user + ("\n修正校验错误：" + str(failure) if failure else ""))
            validate_synthesis(result)
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, json.dumps(result, ensure_ascii=False, indent=2))
            return result
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            LOG.warning("日报综合 JSON 格式或结构校验失败 attempt=%s/2", attempt + 1, exc_info=True)
            failure = error
    raise ValueError("日报综合未通过 JSON 格式或结构校验：" + str(failure))
