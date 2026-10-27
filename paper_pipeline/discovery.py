"""Model-driven web search/fetch; arXiv only resolves discovered IDs."""
import copy
import hashlib
import http.client
import html
import ipaddress
import json
import logging
import re
import string
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from .arxiv import ArxivClient, ENDPOINT, arxiv_id, date_boundary, iso, parse_atom, retry_after, timestamp, utcnow
from .providers import DeepSeekSearch, OfficialAPIError, OfficialClient, OfficialTransportError, redact_secrets
from .publishing import atomic_write
from .runtime import execute_graph, get_task
from .storage import (defer_metadata, dumps, fail_run, finish_run, pending_metadata,
                      queue_metadata, run_summary, save_paper, start_run)
from .web_evidence import FailedPages, PAGE_TTL, TRANSIENT_TTL, page_problem

LOG = logging.getLogger(__name__)
PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
_COMPANY_RULE = (PROMPTS / "company_rule.txt").read_text(encoding="utf-8").strip()
_discovery_prompt = (PROMPTS / "paper_discovery.txt").read_text(encoding="utf-8").replace(
    "{company_rule}", _COMPANY_RULE)
PROMPT, FINAL_PROMPT = (part.strip() for part in _discovery_prompt.split("=== 预算耗尽时的输出要求 ==="))


def tool(name, description, properties, required):
    return {"type": "function", "function": {"name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": required, "additionalProperties": False}}}


TOOLS = [
    tool("web_search", "搜索公开网页，返回 URL、摘要与日期；根据结果调整下一次查询。查询最多 128 字符。",
         {"query": {"type": "string"}}, ["query"]),
    tool("web_fetch", "读取公开网页，返回 Markdown 正文与可用性提示。优先沿链接访问论文或公司页，可由已知 arXiv ID 构造论文 URL；失败或空列表后换来源。",
         {"url": {"type": "string"}}, ["url"]),
    tool("submit_papers", "结束检索，提交有实际来源的候选论文。日期、版本由 arXiv 元数据核对。",
         {"summary": {"type": "string"}, "papers": {"type": "array", "items": {
             "type": "object", "properties": {
                 "arxiv_id": {"type": "string"}, "source_url": {"type": "string"},
                 "bucket": {"type": "string", "enum": ["recsys", "related_ir", "llm_for_ir"]},
                 "priority": {"type": "integer", "minimum": 1, "maximum": 5},
                 "reason": {"type": "string"}},
             "required": ["arxiv_id", "source_url", "bucket", "priority", "reason"],
             "additionalProperties": False}}}, ["summary", "papers"]),
]


def discovery_window(args, config):
    now = utcnow()
    until = min(date_boundary(args.until, end=True), now) if args.until else now
    since = date_boundary(args.since) if args.since else until - timedelta(days=config["digest"]["lookback_windows_days"][0])
    if since >= until:
        raise ValueError("--since must be earlier than --until/current time (UTC)")
    return since, until


def web_url(value):
    if not isinstance(value, str) or len(value) > 8192 or any(c.isspace() for c in value):
        raise ValueError("Expected a public HTTP(S) URL")
    url = urllib.parse.urlsplit(value)
    host = (url.hostname or "").lower()
    if (url.scheme not in {"http", "https"} or not host or url.username or url.password
            or url.port not in {None, 80, 443} or "." not in host
            or host.endswith((".localhost", ".local", ".internal"))):
        raise ValueError("Expected a public HTTP(S) URL without credentials")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("Private addresses cannot be fetched")
    return urllib.parse.urlunsplit(url._replace(fragment=""))


class SearchAgent:
    def __init__(self, config, provider, cache):
        self.config, self.settings, self.cache = config, config["discovery"], cache
        self.client, self.web = OfficialClient(config, provider), DeepSeekSearch(config)
        self.sources, self.memo = {}, {}
        # Daily lookback windows have separate run directories but share failed URLs.
        memory_dir = cache.parent if cache.parent.name == "raw" and re.fullmatch(r"run-\d+", cache.name) else cache
        self.failed_pages = FailedPages(memory_dir / "fetch-failures.json")
        self.excluded_ids = set()
        self.searches, self.fetches, self.calls = 0, 0, 0
        self.trace = {"provider": self.client.provider, "model": self.client.model,
                      "web_provider": "deepseek", "rounds": [], "tools": []}

    def write_trace(self):
        atomic_write(self.cache / "agent-trace.json", redact_secrets(dumps(self.trace)))

    def observe(self, text, source=None):
        # Fetch returns Markdown, which may escape punctuation even in arXiv IDs.
        text = re.sub(r"\\([" + re.escape(string.punctuation) + r"])", r"\1", html.unescape(text))
        links = re.findall(r'https?://[^\s<>"\)\]\}\\]+', text)
        if source:
            links.append(source)
            links.extend(urllib.parse.urljoin(source, target) for target in re.findall(r'\]\(\s*<?([^\s)>]+)', text))
        for raw in links:
            try:
                url = web_url(raw.rstrip(".,;"))
            except ValueError:
                continue
            try:
                identifier, _ = arxiv_id(url)
            except ValueError:
                continue
            self.sources.setdefault(identifier, set()).update([url, source or url])
        if source:
            for raw in re.findall(r"arxiv:\s*(\d{4}\.\d{4,5}(?:v\d+)?|[a-zA-Z.-]+/\d{7}(?:v\d+)?)", text, re.I):
                identifier, _ = arxiv_id(raw)
                self.sources.setdefault(identifier, set()).add(source)

    def submit(self, args):
        if set(args) != {"summary", "papers"} or not isinstance(args["summary"], str) or not args["summary"].strip():
            raise ValueError("submit_papers requires summary and papers")
        papers = args["papers"]
        if not isinstance(papers, list) or len(papers) > self.settings["max_candidates"]:
            raise ValueError("papers must be an array within max_candidates")
        if not self.searches or (papers and not self.fetches):
            raise ValueError("Search first; read at least one page before submitting nonempty papers")
        selected = {}
        for paper in papers:
            if not isinstance(paper, dict) or set(paper) != {"arxiv_id", "source_url", "bucket", "priority", "reason"}:
                raise ValueError("Each paper requires arxiv_id, source_url, bucket, priority, reason")
            if not isinstance(paper["arxiv_id"], str):
                raise ValueError("arxiv_id must be a string")
            identifier, _ = arxiv_id(paper["arxiv_id"])
            source = web_url(paper["source_url"])
            if source not in self.sources.get(identifier, set()):
                raise ValueError(f"{identifier}: source_url must be a tool-observed source containing this ID")
            if (not isinstance(paper["bucket"], str) or paper["bucket"] not in {"recsys", "related_ir", "llm_for_ir"}
                    or type(paper["priority"]) is not int or not 1 <= paper["priority"] <= 5
                    or not isinstance(paper["reason"], str) or not paper["reason"].strip()):
                raise ValueError("Invalid bucket, priority or reason")
            if identifier in self.excluded_ids:
                LOG.info("Agent 跳过已处理或已收录论文 arxiv_id=%s", identifier)
                continue
            selected[identifier] = {**paper, "arxiv_id": identifier, "source_url": source,
                                    "reason": paper["reason"][:2000]}
        return {"summary": args["summary"][:4000], "papers": list(selected.values())}

    def execute(self, name, args):
        if name == "submit_papers":
            return self.submit(args)
        if name not in {"web_search", "web_fetch"}:
            raise ValueError("Unknown tool; use web_search, web_fetch or submit_papers")
        key = dumps([name, args])
        if key in self.memo:
            return {**self.memo[key], "cached": True}
        if name == "web_fetch" and set(args) == {"url"}:
            cached_failure = self.failed_pages.get(web_url(args["url"]))
            if cached_failure:
                return cached_failure
        if self.calls >= self.settings["max_tool_calls"]:
            raise ValueError("Web tool budget exhausted; submit the supported candidates now")
        self.calls += 1
        if name == "web_search":
            if set(args) != {"query"} or not isinstance(args["query"], str) or not 1 <= len(args["query"].strip()) <= 128:
                raise ValueError("web_search requires a query of 1..128 characters")
            results = self.web.search(args["query"], self.config["search"]["count"])
            cleaned = []
            for entry in results[:self.config["search"]["count"]]:
                if not isinstance(entry, dict):
                    continue
                try:
                    url = web_url(entry.get("url"))
                except ValueError:
                    continue
                item = {key: str(entry.get(key) or "")[:1500] for key in ("title", "summary", "snippet", "date")}
                item["url"] = url
                self.observe("\n".join(item.values()), url)
                cleaned.append(item)
            self.searches += 1
            result = {"results": cleaned}
        else:
            if set(args) != {"url"}:
                raise ValueError("web_fetch requires url")
            url = web_url(args["url"])
            page = self.web.fetch(url)
            content = page["content"][:self.settings["max_page_chars"]]
            result = {"url": url, "title": str(page.get("title") or "")[:500], "content": content,
                      "truncated": len(page["content"]) > len(content), "usable": True}
            problem = page_problem(page["content"])
            if problem:
                result.update(usable=False, page_status=problem[0], warning=problem[1], next_action=problem[1])
                self.failed_pages.remember(url, result, PAGE_TTL)
                LOG.warning("Agent 网页不能作为完整证据 url=%s reason=%s", url, problem[0])
            else:
                self.observe(content, url)
                self.fetches += 1
        self.memo[key] = result
        return result

    def finalize(self, task):
        """Finish from observed evidence without exposing any more web tools."""
        LOG.info("Agent 检索预算已用尽，开始整理已有资料 observed_papers=%s", len(self.sources))
        evidence = [{"tool": json.loads(key)[0], "arguments": json.loads(key)[1], "result": value}
                    for key, value in self.memo.items()]
        user = dumps({"task": task, "observed_sources": {key: sorted(value) for key, value in self.sources.items()},
                      "evidence": evidence, "schema": TOOLS[-1]["function"]["parameters"]})
        correction = ""
        self.trace["finalization"] = []
        for attempt in range(2):
            record = {"attempt": attempt + 1}
            self.trace["finalization"].append(record)
            try:
                result, invocation = self.client.json(FINAL_PROMPT, user + correction)
                record.update(result=result, invocation=invocation)
                submission = self.submit(result)
                self.trace["status"] = "completed"
                LOG.info("Agent 资料整理完成 candidates=%s", len(submission["papers"]))
                return submission
            except ValueError as error:
                record["error"] = redact_secrets(str(error))
                LOG.warning("Agent 候选整理格式或来源检查失败 attempt=%s/2", attempt + 1, exc_info=True)
                correction = "\n上次结果无效，请修正；只使用 observed_sources 中的 ID 与对应来源：" + record["error"]
                if attempt == 1:
                    raise RuntimeError("Agent could not produce a valid final selection; inspect agent-trace.json") from error
            finally:
                self.write_trace()

    def snapshot(self, state):
        return {**state, "sources": {key: sorted(value) for key, value in self.sources.items()},
                "memo": copy.deepcopy(self.memo), "searches": self.searches,
                "fetches": self.fetches, "calls": self.calls, "trace": copy.deepcopy(self.trace)}

    def restore(self, state):
        self.sources = {key: set(value) for key, value in state["sources"].items()}
        self.memo, self.trace = copy.deepcopy(state["memo"]), copy.deepcopy(state["trace"])
        self.searches, self.fetches, self.calls = state["searches"], state["fetches"], state["calls"]

    def model_step(self, state):
        self.restore(state)
        step = state["round"] + 1
        if self.messages is None:
            # A fresh conversation can use saved evidence without persisting private reasoning.
            context = {"task": state["task"], "previous_tools": self.trace["tools"],
                       "observed_sources": state["sources"], "recent_failed_pages": self.failed_pages.hints()}
            self.messages = [{"role": "system", "content": PROMPT},
                             {"role": "user", "content": dumps(context)}]
        LOG.info("Agent 检索轮次=%s/%s web_calls=%s/%s", step, self.settings["max_rounds"],
                 self.calls, self.settings["max_tool_calls"])
        message, invocation = self.client.tools(self.messages, TOOLS, self.config["llm"]["max_tokens"])
        self.messages.append(message)  # Provider reasoning stays only in this process.
        calls = message.get("tool_calls", [])
        self.trace["rounds"].append({"round": step, "invocation": invocation,
                                     "tool_names": [call["function"]["name"] for call in calls]})
        if not calls:
            self.messages.append({"role": "user", "content": "请继续调用工具；完成后必须用 submit_papers 提交结果。"})
        self.write_trace()
        return self.snapshot({**state, "round": step, "pending": calls, "tool_index": 0})

    def tool_step(self, state):
        self.restore(state)
        call = state["pending"][state["tool_index"]]
        name = call["function"]["name"]
        event = {"round": state["round"], "name": name, "arguments": call["function"]["arguments"]}
        self.trace["tools"].append(event)
        LOG.info("Agent 工具开始 name=%s arguments=%s", name, redact_secrets(event["arguments"])[:3000])
        try:
            args = json.loads(call["function"]["arguments"])
            if not isinstance(args, dict):
                raise ValueError("Tool arguments must be a JSON object")
            if name == "submit_papers" and len(state["pending"]) != 1:
                raise ValueError("Call submit_papers alone after reading all tool results")
            result = self.execute(name, args)
        except Exception as error:
            recoverable = isinstance(error, ValueError)
            if isinstance(error, OfficialAPIError):
                statuses = {400, 404, 422} | ({403, 408, 429, 500, 502, 503, 504} if name == "web_fetch" else set())
                recoverable = error.status in statuses
            if name == "web_fetch" and isinstance(error, (OfficialTransportError, TimeoutError, ConnectionError)):
                recoverable = True
            result = {"error": redact_secrets(str(error)), "recoverable": recoverable}
            if isinstance(error, OfficialAPIError):
                result["http_status"] = error.status
            if name == "web_fetch":
                result.update(usable=False, next_action="该 URL 暂时无法读取，不代表论文不存在；请换论文原文、公司页等来源。")
                if recoverable and isinstance(error, (OfficialAPIError, OfficialTransportError, TimeoutError, ConnectionError)):
                    url = web_url(args["url"])
                    result["url"] = url
                    ttl = PAGE_TTL if result.get("http_status") in {400, 403, 404, 422} else TRANSIENT_TTL
                    self.failed_pages.remember(url, result, ttl)
            event["result"] = result
            self.write_trace()
            if not recoverable:
                LOG.error("Agent 工具出现不可恢复错误 name=%s", name, exc_info=True)
                raise
            LOG.warning("Agent 工具失败 name=%s，返回错误供模型调整", name, exc_info=True)
        event["result"] = result
        self.write_trace()
        LOG.info("Agent 工具完成 name=%s error=%s usable=%s", name, "error" in result, result.get("usable"))
        progress = {"rounds_left": self.settings["max_rounds"] - state["round"],
                    "web_calls_left": max(0, self.settings["max_tool_calls"] - self.calls),
                    "observed_papers": len(self.sources),
                    "next_step": "资料足够时提交候选；日期由后续 arXiv 元数据核对，不必为凑满数量持续翻页。"}
        if self.messages is not None:
            self.messages.append({"role": "tool", "tool_call_id": call["id"],
                                  "content": dumps({**result, "agent_progress": progress})})
        state = {**state, "tool_index": state["tool_index"] + 1}
        if name == "submit_papers" and "error" not in result:
            self.trace["status"] = "completed"
            state["result"] = result
        return self.snapshot(state)

    def final_step(self, state):
        self.restore(state)
        return self.snapshot({**state, "result": self.finalize(state["task"])})

    def route(self, state):
        if state.get("result") is not None:
            return END
        if state["tool_index"] < len(state["pending"]):
            return "tool"
        if state["round"] >= self.settings["max_rounds"] or state["calls"] >= self.settings["max_tool_calls"]:
            return "finalize"
        return "model"

    def run(self, since, until, target, excluded_ids=None):
        self.excluded_ids = set(excluded_ids or [])
        task = {"since_utc": iso(since), "until_utc_exclusive": iso(until), "date_field": "updated",
                "preferred_companies": self.config["preferred_companies"], "target_papers": target,
                "target_companies": self.config["companies"], "company_only": True,
                "excluded_arxiv_ids": sorted(self.excluded_ids),
                **{key: self.settings[key] for key in ("max_rounds", "max_tool_calls", "max_candidates")}}
        self.trace["task"] = task
        self.messages = None
        builder = StateGraph(dict)
        builder.add_node("model", self.model_step)
        builder.add_node("tool", self.tool_step)
        builder.add_node("finalize", self.final_step)
        builder.add_conditional_edges(START, self.route)
        builder.add_conditional_edges("model", self.route)
        builder.add_conditional_edges("tool", self.route)
        builder.add_edge("finalize", END)
        state = self.snapshot({"task": task, "round": 0, "pending": [], "tool_index": 0, "result": None})
        saved = get_task(self.cache, "search")
        if saved and saved["request"]["task"] != task:
            raise ValueError("Search checkpoint belongs to a different request; use a new run directory")
        try:
            _, state = execute_graph(builder, state, directory=self.cache, kind="search", request={"task": task},
                                     task_id="search", resume=saved is not None)
            self.restore(state)
            return state["result"]
        except Exception as error:
            self.trace.update(status="failed", error=redact_secrets(str(error)))
            LOG.exception("Agent 检索失败")
            raise
        finally:
            self.write_trace()


def agent_selection(paper, decision, config):
    """Selection comes straight from the agent's bucket/priority; companies still require evidence."""
    preferred = sorted(set(paper["companies"]) & set(config["preferred_companies"]))
    score = {"recsys": 100, "related_ir": 50, "llm_for_ir": 40}[decision["bucket"]] + decision["priority"] * 2
    return {"selected": True, "bucket": decision["bucket"], "score": score,
            "tags": [], "reasons": [decision["reason"]], "matched_terms": [],
            "preferred_companies": preferred,
            "team_priority": "preferred" if preferred else ("other" if paper["companies"] else "unknown"),
            "text_basis": "agent_web_evidence", "method": "agent", "agent_decision": decision}


def import_pending_metadata(connection, directory):
    """Recover pre-queue failures from their already-validated agent traces, once."""
    recovered = []
    rows = connection.execute("SELECT * FROM runs WHERE kind='agent_search' AND summary_json IS NOT NULL").fetchall()
    for run in rows:
        marker = f"pending_metadata_import:{run['id']}"
        if connection.execute("SELECT 1 FROM state WHERE key=?", (marker,)).fetchone():
            continue
        summary = json.loads(run["summary_json"])
        failed = set(summary.get("metadata_failed", []))
        if not failed:
            continue
        path = Path(directory) / "raw" / f"run-{run['id']:06d}" / "agent-trace.json"
        try:
            trace = json.loads(path.read_text(encoding="utf-8"))
            results = [event.get("result", {}) for event in trace.get("tools", [])
                       if event.get("name") == "submit_papers"]
            results += [event.get("result", {}) for event in trace.get("finalization", [])]
            submitted = next(value for value in reversed(results) if isinstance(value.get("papers"), list))
            decisions = {item["arxiv_id"]: item for item in submitted["papers"]}
            if not failed <= decisions.keys():
                raise ValueError("Trace does not contain every failed metadata candidate")
            for identifier in failed:
                decision = decisions[identifier]
                if arxiv_id(identifier)[0] != identifier or not decision.get("reason"):
                    raise ValueError("Invalid saved candidate")
                web_url(decision["source_url"])
                if decision["bucket"] not in {"recsys", "related_ir", "llm_for_ir"}:
                    raise ValueError("Invalid saved candidate bucket")
                if type(decision.get("priority")) is not int or not 1 <= decision["priority"] <= 5:
                    raise ValueError("Invalid saved candidate priority")
            with connection:
                for identifier in sorted(failed):
                    if connection.execute("SELECT 1 FROM papers WHERE arxiv_id=?", (identifier,)).fetchone():
                        continue
                    queue_metadata(connection, run["id"], decisions[identifier],
                                   timestamp(run["since_utc"]), timestamp(run["until_utc"]))
                    recovered.append(identifier)
                connection.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (marker, iso(utcnow())))
                summary["agent_candidates"] = max(summary.get("agent_candidates", 0), len(decisions))
                summary.setdefault("metadata_verified", summary.get("unique_seen", 0) + len(summary.get("outside_window", [])))
                connection.execute("UPDATE runs SET status='partial',summary_json=? WHERE id=?",
                                   (dumps(summary), run["id"]))
        except (OSError, ValueError, KeyError, TypeError, StopIteration):
            LOG.warning("历史候选恢复失败 run_id=%s trace=%s", run["id"], path, exc_info=True)
    if recovered:
        LOG.info("从历史轨迹恢复待验证元数据候选 count=%s ids=%s", len(recovered), recovered)
    return recovered


def _candidate_metadata(config, cache, connection, decisions):
    """Resolve IDs with a bounded retry, retaining failures and partial batches."""
    identifiers, papers, failed = sorted(decisions), [], []
    arxiv = ArxivClient(config, cache, metadata=True)
    for start in range(0, len(identifiers), 50):
        batch = identifiers[start:start + 50]
        key = hashlib.sha256(",".join(batch).encode()).hexdigest()[:20]
        path = cache / f"ids-{key}.xml"
        cooldown = connection.execute("SELECT value FROM state WHERE key='arxiv_metadata_retry_at'").fetchone()
        if not path.exists() and cooldown and timestamp(cooldown[0]) > utcnow():
            with connection:
                defer_metadata(connection, batch, "arXiv metadata endpoint cooling down", timestamp(cooldown[0]), attempted=False)
            failed.extend(batch)
            LOG.info("arXiv 元数据冷却期间保留候选 ids=%s next_retry_at=%s", batch, cooldown[0])
            continue
        url = ENDPOINT + "?" + urllib.parse.urlencode({"id_list": ",".join(batch), "max_results": len(batch)})
        try:
            raw = path.read_bytes() if path.exists() else arxiv.request(url)
            parsed, _, _ = parse_atom(raw, config)
            returned = {paper["arxiv_id"] for paper in parsed}
            if not returned <= set(batch):
                raise ValueError("arXiv metadata returned unexpected IDs")
            papers.extend(parsed)
            missing = sorted(set(batch) - returned)
            if missing:
                with connection:
                    defer_metadata(connection, missing, "arXiv metadata omitted these IDs", utcnow() + timedelta(minutes=5))
                failed.extend(missing)
                LOG.warning("arXiv 元数据响应缺少 ID；已保留重试 ids=%s", missing)
            else:
                path.write_bytes(raw)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ConnectionError,
                http.client.HTTPException, ET.ParseError, ValueError) as error:
            delay = max(300, retry_after((error.headers or {}).get("Retry-After"))) if isinstance(error, urllib.error.HTTPError) else 300
            with connection:
                defer_metadata(connection, batch, redact_secrets(str(error)), utcnow() + timedelta(seconds=delay))
                if isinstance(error, urllib.error.HTTPError) and (error.code == 429 or retry_after((error.headers or {}).get("Retry-After")) > 5):
                    connection.execute("INSERT OR REPLACE INTO state VALUES ('arxiv_metadata_retry_at',?)",
                                       (iso(utcnow() + timedelta(seconds=delay)),))
            failed.extend(batch)
            LOG.warning("arXiv 元数据批次失败，已保留 %s 个候选供重试：%s", len(batch), redact_secrets(str(error)), exc_info=True)
    return papers, failed


def _save_agent_candidate(connection, run_id, paper, decision, config, cache, provider=None, model=None):
    save_paper(connection, run_id, paper, config,
               selection_override=agent_selection(paper, decision, config))
    connection.execute("INSERT OR REPLACE INTO provenance VALUES (?,?,?,?)",
        (paper["arxiv_id"], "agent_search", decision["source_url"],
         dumps({"decision": decision, "provider": provider, "model": model, "run_id": run_id,
                "trace": str(cache / "agent-trace.json"), "recorded_at": iso(utcnow())})))


def recover_pending_metadata(args, config, connection, since, until, excluded_ids=(), force=False, candidate_ids=None):
    """Retry saved decisions without a model/search call; daily supplies its full window."""
    imported = import_pending_metadata(connection, args.db.parent)
    excluded, now = set(excluded_ids), utcnow()
    wanted = None if candidate_ids is None else set(candidate_ids)
    jobs = [job for job in pending_metadata(connection)
            if job["arxiv_id"] not in excluded and (wanted is None or job["arxiv_id"] in wanted)
            and timestamp(job["since_utc"]) < until and timestamp(job["until_utc"]) > since
            and (force or not job["next_retry_at"] or timestamp(job["next_retry_at"]) <= now)]
    jobs = jobs[:config["discovery"]["max_candidates"]]
    decisions = {job["arxiv_id"]: job["decision"] for job in jobs}
    output = {"kind": "metadata_recovery", "attempted_ids": list(decisions), "imported_ids": imported,
              "metadata_verified": 0, "metadata_failed": [], "outside_window": [],
              "unique_seen": 0, "selected_seen": 0, "pending_ids": [job["arxiv_id"] for job in pending_metadata(connection)]}
    cooldown = connection.execute("SELECT value FROM state WHERE key='arxiv_metadata_retry_at'").fetchone()
    if cooldown and timestamp(cooldown[0]) > now:
        return {**output, "status": "deferred", "attempted_ids": [], "next_retry_at": cooldown[0]}
    if not jobs:
        return {**output, "status": "deferred" if output["pending_ids"] else "completed"}
    papers, failed = _candidate_metadata(config, args.db.parent / "raw" / "metadata-recovery", connection, decisions)
    by_id = {paper["arxiv_id"]: paper for paper in papers}
    accepted, outside = [], []
    with connection:
        for run_id in sorted({job["run_id"] for job in jobs}):
            run = connection.execute("SELECT summary_json FROM runs WHERE id=?", (run_id,)).fetchone()
            summary = json.loads(run[0] or "{}")
            run_jobs = [job for job in jobs if job["run_id"] == run_id]
            verified = [by_id[job["arxiv_id"]] for job in run_jobs if job["arxiv_id"] in by_id]
            for paper in verified:
                identifier = paper["arxiv_id"]
                if since <= timestamp(paper["updated"]) < until:
                    _save_agent_candidate(connection, run_id, paper, decisions[identifier], config,
                                          args.db.parent / "raw" / f"run-{run_id:06d}")
                    accepted.append(identifier)
                else:
                    outside.append(identifier)
                connection.execute("DELETE FROM pending_metadata WHERE arxiv_id=?", (identifier,))
            remaining = [row[0] for row in connection.execute("SELECT arxiv_id FROM pending_metadata WHERE run_id=? ORDER BY arxiv_id", (run_id,))]
            summary.update({**run_summary(connection, run_id),
                            "agent_candidates": max(summary.get("agent_candidates", 0), len(run_jobs)),
                            "metadata_verified": summary.get("metadata_verified", 0) + len(verified),
                            "metadata_failed": remaining,
                            "outside_window": sorted(set(summary.get("outside_window", [])) | (set(outside) & {job["arxiv_id"] for job in run_jobs})),
                            "metadata_recovered_at": iso(utcnow())})
            finish_run(connection, run_id, summary)
    pending = [job["arxiv_id"] for job in pending_metadata(connection)]
    LOG.info("待验证元数据恢复 attempted=%s verified=%s accepted=%s failed=%s pending=%s", len(jobs), len(papers), len(accepted), len(failed), len(pending))
    return {**output, "status": "partial" if failed else "completed", "metadata_verified": len(papers),
            "metadata_failed": failed, "outside_window": outside, "unique_seen": len(accepted),
            "selected_seen": len(accepted), "pending_ids": pending}


def discover(args, config, connection):
    child_id = getattr(args, "discovery_task_id", None)
    task_id = child_id or getattr(args, "task_id", None) or uuid4().hex
    saved = get_task(args.db.parent, task_id)
    resume = bool(saved) if child_id else bool(getattr(args, "resume", False))
    if saved and not resume:
        raise ValueError(f"Task {task_id} already exists; use resume")
    if resume and not saved:
        raise ValueError(f"Unknown task: {task_id}")
    if saved:
        request = saved["request"]
        config = request["config"]
        since, until = timestamp(request["since"]), timestamp(request["until"])
        run_id = request["run_id"]
    else:
        since, until = discovery_window(args, config)
        run_id = None
    # A finished graph may still contain deferred metadata. Explicit discover
    # resume repairs its saved candidates, without executing the search graph.
    if saved and saved["status"] == "completed" and (not child_id or getattr(args, "resume", False)):
        original = saved["result"] or {}
        if original.get("metadata_failed"):
            recovery = recover_pending_metadata(args, config, connection, since, until,
                                                candidate_ids=original["metadata_failed"])
            row = connection.execute("SELECT status,summary_json FROM runs WHERE id=?", (run_id,)).fetchone()
            output = {**original, **json.loads(row["summary_json"]), "status": row["status"], "metadata_recovery": recovery}
            with connection:
                connection.execute("UPDATE agent_tasks SET result_json=?,updated_at=? WHERE task_id=?",
                                   (dumps(output), iso(utcnow()), task_id))
            return output
        return original
    client = OfficialClient(config, args.provider)
    client.validate_generation()
    client.require_key()
    DeepSeekSearch(config).client.require_key()
    if run_id is None:
        run_id = start_run(connection, "agent_search", config, since, until)
        request = {"args": {key: str(value.resolve()) if isinstance(value, Path) else value for key, value in vars(args).items()},
                   "config": config, "run_id": run_id, "since": iso(since), "until": iso(until)}
    cache = args.db.parent / "raw" / f"run-{run_id:06d}"
    cache.mkdir(parents=True, exist_ok=True)
    excluded = set(getattr(args, "excluded_ids", []))
    LOG.info("Agent 检索开始 task_id=%s run_id=%s provider=%s since=%s until=%s", task_id, run_id, client.provider, since, until)

    def search(state):
        agent = SearchAgent(config, args.provider, cache)
        pending_ids = {job["arxiv_id"] for job in pending_metadata(connection)}
        previous_search = get_task(cache, "search")
        search_excluded = (set(previous_search["request"]["task"]["excluded_arxiv_ids"])
                           if previous_search else excluded | pending_ids)
        result = agent.run(since, until, min(getattr(args, "limit", None) or config["digest"]["max_papers"],
                                            config["discovery"]["max_candidates"]), excluded_ids=search_excluded)
        return {**state, "result": result}

    def metadata(state):
        decisions = {paper["arxiv_id"]: paper for paper in state["result"]["papers"] if paper["arxiv_id"] not in excluded}
        with connection:
            for decision in decisions.values():
                queue_metadata(connection, run_id, decision, since, until)
        papers, failed = _candidate_metadata(config, cache, connection, decisions)
        accepted = [paper for paper in papers if since <= timestamp(paper["updated"]) < until]
        outside = [paper["arxiv_id"] for paper in papers if not since <= timestamp(paper["updated"]) < until]
        if not accepted:
            LOG.warning("Agent 暂无日期范围内的可入库论文；提交候选=%s，元数据成功=%s，待重试=%s，日期排除=%s",
                        len(decisions), len(papers), len(failed), len(outside))
        return {**state, "decisions": decisions, "accepted": accepted, "outside": outside,
                "candidate_count": len(decisions), "metadata_verified": len(papers), "metadata_failed": failed}

    def ingest(state):
        row = connection.execute("SELECT status, summary_json FROM runs WHERE id=?", (run_id,)).fetchone()
        with connection:
            # A committed ingestion can be replayed if its graph checkpoint was interrupted.
            if row["status"] in {"completed", "partial"}:
                summary = json.loads(row["summary_json"])
            else:
                for paper in state["accepted"]:
                    decision = state["decisions"][paper["arxiv_id"]]
                    _save_agent_candidate(connection, run_id, paper, decision, config, cache, client.provider, client.model)
                resolved = [paper["arxiv_id"] for paper in state["accepted"]] + state["outside"]
                connection.executemany("DELETE FROM pending_metadata WHERE arxiv_id=?", [(identifier,) for identifier in resolved])
                summary = {**run_summary(connection, run_id), "agent_candidates": state["candidate_count"],
                           "metadata_verified": state.get("metadata_verified", len(state["accepted"]) + len(state["outside"])),
                           "outside_window": state["outside"], "metadata_failed": state["metadata_failed"],
                           "search_summary": state["result"]["summary"]}
                if state["metadata_failed"]:
                    connection.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (f"pending_metadata_import:{run_id}", iso(utcnow())))
                finish_run(connection, run_id, summary)
        LOG.info("Agent 检索入库完成 run_id=%s submitted=%s verified=%s accepted=%s metadata_pending=%s", run_id,
                 summary["agent_candidates"], summary["metadata_verified"], summary["unique_seen"], len(summary["metadata_failed"]))
        return {**state, "output": {"task_id": task_id, "run_id": run_id, "kind": "agent_search", **summary,
                "status": "partial" if summary["metadata_failed"] else "completed",
                "since_utc": iso(since), "until_utc": iso(until), "trace": str(cache / "agent-trace.json")}}

    builder = StateGraph(dict)
    for name, operation in (("search", search), ("metadata", metadata), ("ingest", ingest)):
        builder.add_node(name, operation)
    builder.add_edge(START, "search")
    builder.add_edge("search", "metadata")
    builder.add_edge("metadata", "ingest")
    builder.add_edge("ingest", END)
    try:
        _, state = execute_graph(builder, {}, directory=args.db.parent, kind="discover", request=request,
                                 task_id=task_id, resume=resume)
        return state["output"]
    except Exception as error:
        connection.rollback()
        if connection.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()[0] not in {"completed", "partial"}:
            fail_run(connection, run_id, error)
        raise
