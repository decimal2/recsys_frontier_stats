"""Model-driven web search/fetch; arXiv only resolves discovered IDs."""
import copy
import html
import ipaddress
import json
import logging
import re
import string
import urllib.parse
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from .arxiv import ArxivClient, ENDPOINT, arxiv_id, date_boundary, iso, parse_atom, timestamp, utcnow
from .providers import DeepSeekSearch, OfficialAPIError, OfficialClient, redact_secrets
from .publishing import atomic_write
from .runtime import execute_graph, get_task
from .storage import dumps, fail_run, finish_run, run_summary, save_paper, start_run

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
    tool("web_fetch", "读取公开网页，返回 Markdown 正文。可以沿链接访问、构造分页 URL，或由已知 arXiv ID 构造论文 URL。",
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
                      "truncated": len(page["content"]) > len(content)}
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
                       "observed_sources": state["sources"]}
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
        except (ValueError, OfficialAPIError) as error:
            if isinstance(error, OfficialAPIError):
                recoverable = {400, 404, 422} | ({403} if name == "web_fetch" else set())
                if error.status not in recoverable:
                    raise
            LOG.warning("Agent 工具失败 name=%s，返回错误供模型调整", name, exc_info=True)
            result = {"error": redact_secrets(str(error))}
        event["result"] = result
        self.write_trace()
        LOG.info("Agent 工具完成 name=%s error=%s", name, "error" in result)
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
        result = agent.run(since, until, min(getattr(args, "limit", None) or config["digest"]["max_papers"],
                                            config["discovery"]["max_candidates"]), excluded_ids=excluded)
        return {**state, "result": result}

    def metadata(state):
        decisions = {paper["arxiv_id"]: paper for paper in state["result"]["papers"] if paper["arxiv_id"] not in excluded}
        identifiers, papers, metadata_failed = sorted(decisions), [], []
        arxiv = ArxivClient(config, cache)
        for start in range(0, len(identifiers), 50):
            batch = identifiers[start:start + 50]
            url = ENDPOINT + "?" + urllib.parse.urlencode({"id_list": ",".join(batch), "max_results": len(batch)})
            path = cache / f"ids-{start:04d}.xml"
            try:
                raw = path.read_bytes() if path.exists() else arxiv.request(url)
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ConnectionError) as error:
                # A rate-limited or failed batch must not discard the agent's other candidates.
                LOG.warning("arXiv 元数据批次失败，跳过 %s 个候选：%s", len(batch), error)
                metadata_failed.extend(batch)
                continue
            parsed, _, _ = parse_atom(raw, config)
            if {paper["arxiv_id"] for paper in parsed} != set(batch):
                raise RuntimeError("arXiv metadata did not return all agent-selected IDs")
            path.write_bytes(raw)
            papers.extend(parsed)
        accepted, outside = [], []
        for paper in papers:
            if since <= timestamp(paper["updated"]) < until:
                accepted.append(paper)
            else:
                outside.append(paper["arxiv_id"])
                LOG.info("Agent 候选不在指定日期范围，跳过 arxiv_id=%s updated=%s", paper["arxiv_id"], paper["updated"])
        if not accepted:
            LOG.warning("Agent 未找到日期范围内的可入库论文；候选=%s，日期排除=%s", len(papers), len(outside))
        return {**state, "decisions": decisions, "accepted": accepted, "outside": outside,
                "candidate_count": len(papers), "metadata_failed": metadata_failed}

    def ingest(state):
        row = connection.execute("SELECT status, summary_json FROM runs WHERE id=?", (run_id,)).fetchone()
        with connection:
            # A committed ingestion can be replayed if its graph checkpoint was interrupted.
            if row["status"] == "completed":
                summary = json.loads(row["summary_json"])
            else:
                for paper in state["accepted"]:
                    decision = state["decisions"][paper["arxiv_id"]]
                    save_paper(connection, run_id, paper, config,
                               selection_override=agent_selection(paper, decision, config))
                    connection.execute("INSERT OR REPLACE INTO provenance VALUES (?,?,?,?)",
                        (paper["arxiv_id"], "agent_search", decision["source_url"],
                         dumps({"decision": decision, "provider": client.provider, "model": client.model,
                                "trace": str(cache / "agent-trace.json"), "recorded_at": iso(utcnow())})))
                summary = {**run_summary(connection, run_id), "agent_candidates": state["candidate_count"],
                           "outside_window": state["outside"], "metadata_failed": state["metadata_failed"],
                           "search_summary": state["result"]["summary"]}
                finish_run(connection, run_id, summary)
        LOG.info("Agent 检索入库完成 run_id=%s accepted=%s", run_id, len(state["accepted"]))
        return {**state, "output": {"task_id": task_id, "run_id": run_id, "kind": "agent_search", **summary,
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
        if connection.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()[0] != "completed":
            fail_run(connection, run_id, error)
        raise
