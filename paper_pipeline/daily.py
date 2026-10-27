"""Persistent pending work, full-text insight, daily synthesis and publication to local files."""
import json
import hashlib
import logging
import xml.etree.ElementTree as ET
from argparse import Namespace
from datetime import date, timedelta
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from .arxiv import DAY_ZONE, date_boundary, iso, timestamp, utcnow
from .fulltext import FullTextStore
from .insights import analyze, synthesize
from .providers import OfficialClient
from .selection import organization_details
from .storage import dumps, enqueue
from .runtime import execute_graph

LOG = logging.getLogger(__name__)


def queued(connection):
    result = []
    for row in connection.execute("SELECT * FROM digest_queue"):
        result.append({"paper": json.loads(row["metadata_json"]), "selection": json.loads(row["selection_json"]),
                       "added_at": row["added_at"], "last_error": row["last_error"]})
    return sorted(result, key=lambda p: (-p["selection"]["score"], p["added_at"], p["paper"]["arxiv_id"]))


def in_window(paper, since, until):
    if since is None and until is None:
        return True
    if not paper.get("updated"):
        return False
    updated = timestamp(paper["updated"])
    return (since is None or updated >= since) and (until is None or updated < until)


def _initial_state(args, config, connection, client):
    limit = args.limit if args.limit is not None else config["digest"]["max_papers"]
    minimum = config["digest"]["min_papers"]
    if limit < minimum:
        raise ValueError(f"--limit must be >= digest.min_papers ({minimum})")
    day = args.date or utcnow().astimezone(DAY_ZONE).date().isoformat()
    date.fromisoformat(day)
    saved = connection.execute("SELECT payload_json FROM editions WHERE day=?", (day,)).fetchone()
    previous = json.loads(saved[0]) if saved else {}
    automatic = getattr(args, "command", None) == "daily"
    since_text = getattr(args, "since", None)
    until_text = getattr(args, "until", None)
    if not since_text and not until_text:
        existing_window = previous.get("paper_window") or {}
        since_text, until_text = existing_window.get("since_utc"), existing_window.get("until_utc_exclusive")
    since = date_boundary(since_text) if since_text else None
    until = date_boundary(until_text, end=True) if until_text else None
    if automatic:
        # The reporting date is a cutoff; --since is only a starting hint for daily.
        until = min(date_boundary(getattr(args, "until", None) or day, end=True), utcnow())
        if since is not None and since >= until:
            raise ValueError("--since must be earlier than --until/current time (UTC)")
        since = min(since or until, until - timedelta(days=config["digest"]["lookback_windows_days"][0]))
        old_since = (previous.get("paper_window") or {}).get("since_utc")
        if old_since:
            since = min(since, timestamp(old_since))
    if since is not None and until is not None and since >= until:
        raise ValueError("--since must be earlier than --until (date endpoints include the whole day)")
    if args.run_id is not None:
        if not connection.execute("SELECT 1 FROM runs WHERE id=? AND status='completed'", (args.run_id,)).fetchone():
            raise ValueError("--run-id must identify a completed collection/import run")
        with connection:
            for row in connection.execute("SELECT metadata_json,selection_json FROM run_items WHERE run_id=?", (args.run_id,)):
                enqueue(connection, json.loads(row[0]), json.loads(row[1]))
    other_ids = {item["paper"]["arxiv_id"]
                 for row in connection.execute("SELECT payload_json FROM editions WHERE day<>?", (day,))
                 for item in json.loads(row[0]).get("items", [])}
    items = {item["paper"]["arxiv_id"]: item for item in previous.get("items", [])
             if in_window(item["paper"], since, until) and item["paper"]["arxiv_id"] not in other_ids}
    errors = []
    rejected = list(previous.get("rejected", []))
    for identifier, item in list(items.items()):
        item.update(organization_details(item["paper"], item["insight"]["analysis"], config))
        item["companies"] = item["organizations"]
        item["company_basis"] = item["organization_basis"]
        item["preferred_companies"] = sorted(set(item["companies"]) & set(config["preferred_companies"]))
        if not item["companies"]:
            rejected.append({"arxiv_id": identifier, "reason": item["organization_basis"]})
            del items[identifier]
    LOG.info("日报开始 date=%s provider=%s model=%s existing=%s minimum=%s limit=%s automatic_backfill=%s since=%s until=%s",
             day, client.provider, client.model, len(items), minimum, limit, automatic, since, until)
    windows = list(dict.fromkeys(iso(min(since, until - timedelta(days=days)))
                   for days in config["digest"]["lookback_windows_days"])) if automatic else []
    return {"day": day, "limit": limit, "minimum": minimum,
            "since": iso(since) if since else None, "until": iso(until) if until else None,
            "windows": windows, "window_index": 0, "items": items, "other_ids": sorted(other_ids),
            "window_searched": True,
            "attempted": [], "collections": [], "errors": errors, "rejected": rejected,
            "previous_discovery": previous.get("discovery", []), "search_failed": False,
            "edition_revision": hashlib.sha256(saved[0].encode()).hexdigest() if saved else None}


class DailyState(TypedDict, total=False):
    task_id: str
    day: str
    limit: int
    minimum: int
    since: str | None
    until: str | None
    windows: list[str]
    window_index: int
    window_searched: bool
    items: dict
    other_ids: list[str]
    attempted: list[str]
    collections: list[dict]
    errors: list[dict]
    rejected: list[dict]
    previous_discovery: list[dict]
    edition_revision: str | None
    search_failed: bool
    metadata_recovery: dict
    jobs: list[dict]
    payload: dict
    synthesis_ok: bool
    result: dict


def digest(args, config, connection):
    """Run or resume the serial daily graph; existing CLI and edition rules are shared."""
    limit = args.limit if args.limit is not None else config["digest"]["max_papers"]
    if limit < config["digest"]["min_papers"]:
        raise ValueError(f"--limit must be >= digest.min_papers ({config['digest']['min_papers']})")
    client = OfficialClient(config, args.provider)
    client.require_key()
    fulltexts = FullTextStore(config, args.db.parent / "fulltext")
    cache = args.db.parent / "insights"
    task_id = getattr(args, "task_id", None) or uuid4().hex

    def recover_metadata(state):
        if getattr(args, "command", None) != "daily" or len(state["items"]) >= state["minimum"]:
            return {}
        from .discovery import recover_pending_metadata
        excluded = set(state["other_ids"]) | state["items"].keys() | {
            row[0] for row in connection.execute("SELECT arxiv_id FROM digest_processed")}
        # Resolve old candidates once, before buying another model search. Older
        # papers remain queued until load_queue admits their lookback window.
        since = min(state["windows"]) if state["windows"] else state["since"]
        try:
            result = recover_pending_metadata(args, config, connection, timestamp(since),
                                              timestamp(state["until"]), excluded_ids=excluded)
            return {"metadata_recovery": result}
        except (OSError, ValueError, RuntimeError, ET.ParseError) as error:
            LOG.exception("日报候选元数据恢复失败，保留已有解读 date=%s", state["day"])
            return {"search_failed": True, "errors": state["errors"] + [
                {"arxiv_id": "metadata_recovery", "task_id": task_id, "error": str(error)}]}

    def load_queue(state):
        excluded = set(state["attempted"]) | set(state["other_ids"]) | state["items"].keys()
        since = timestamp(state["since"]) if state["since"] else None
        until = timestamp(state["until"]) if state["until"] else None
        jobs = [job for job in queued(connection) if job["paper"]["arxiv_id"] not in excluded
                and in_window(job["paper"], since, until)]
        return {"jobs": jobs[:config["discovery"]["max_candidates"]]}

    def next_step(state):
        if state["jobs"] and len(state["items"]) < state["limit"]:
            return "read_paper"
        if len(state["items"]) < state["minimum"] and not state["search_failed"]:
            if not state.get("window_searched", True):
                return "discover"
            if state["window_index"] < len(state["windows"]):
                return "expand_window"
        return "synthesize"

    def expand_window(state):
        return {"since": state["windows"][state["window_index"]],
                "window_index": state["window_index"] + 1, "window_searched": False}

    def search(state):
        from .discovery import discover
        excluded = set(state["attempted"]) | set(state["other_ids"]) | state["items"].keys() | {
            row[0] for row in connection.execute("SELECT arxiv_id FROM digest_processed")}
        search_args = Namespace(**{**vars(args), "since": state["since"], "until": state["until"],
            "excluded_ids": sorted(excluded), "task_id": None, "resume": False,
            "discovery_task_id": f"{task_id}-search-{state['window_index']}",
            "limit": min(config["discovery"]["max_candidates"], 2 * (state["limit"] - len(state["items"])))})
        LOG.info("日报补齐检索 round=%s collected=%s minimum=%s missing=%s since=%s until=%s exclude=%s",
                 state["window_index"], len(state["items"]), state["minimum"],
                 state["minimum"] - len(state["items"]), state["since"], state["until"], len(excluded))
        try:
            collection = discover(search_args, config, connection)
            return {"collections": state["collections"] + [collection], "window_searched": True}
        except (OSError, ValueError, RuntimeError, ET.ParseError) as error:
            LOG.exception("日报补齐检索失败，保留已完成解读 date=%s", state["day"])
            return {"search_failed": True, "window_searched": True, "errors": state["errors"] + [{"arxiv_id": "discovery",
                "task_id": search_args.discovery_task_id, "window_index": state["window_index"],
                "since_utc": state["since"], "until_utc": state["until"], "error": str(error)}]}

    def read_paper(state):
        job = state["jobs"][0]
        paper = job["paper"]
        update = {"jobs": state["jobs"][1:], "attempted": state["attempted"] + [paper["arxiv_id"]]}
        LOG.info("论文处理开始 arxiv_id=%sv%s", paper["arxiv_id"], paper["version"])
        try:
            document = fulltexts.get(paper)
            insight = analyze(paper, document, client, config, cache)
            source = organization_details(paper, insight["analysis"], config)
            companies = source["organizations"]
            if companies and companies != sorted(paper.get("companies", [])):
                # Full-text affiliations are the freshest company evidence; keep the papers table in step.
                with connection:
                    connection.execute("UPDATE papers SET metadata_json=? WHERE arxiv_id=?",
                                       (dumps({**paper, "companies": companies}), paper["arxiv_id"]))
            reason = (insight["analysis"]["relevance_reason"] if not insight["analysis"]["in_scope"]
                      else source["organization_basis"] if not companies else None)
            if reason:
                LOG.info("论文不在选题范围 arxiv_id=%s reason=%s", paper["arxiv_id"], reason)
                update["rejected"] = state["rejected"] + [{"arxiv_id": paper["arxiv_id"], "reason": reason}]
                with connection:
                    connection.execute("INSERT OR REPLACE INTO digest_processed VALUES (?,?,?,?)",
                        (paper["arxiv_id"], paper["version"], "out_of_scope", iso(utcnow())))
                    connection.execute("DELETE FROM digest_queue WHERE arxiv_id=?", (paper["arxiv_id"],))
                return update
            item = {"paper": paper, "selection": job["selection"], "insight": insight,
                    "companies": companies, "company_basis": source["organization_basis"], **source}
            LOG.info("机构识别 arxiv_id=%s organizations=%s matched_companies=%s",
                     paper["arxiv_id"], item["organizations"], item["companies"])
            item["preferred_companies"] = sorted(set(item["companies"]) & set(config["preferred_companies"]))
            update["items"] = {**state["items"], paper["arxiv_id"]: item}
            LOG.info("论文解读完成 arxiv_id=%s", paper["arxiv_id"])
        except (OSError, ValueError, RuntimeError, ImportError) as error:
            LOG.exception("论文处理失败，保留待处理任务 arxiv_id=%s", paper["arxiv_id"])
            update["errors"] = state["errors"] + [{"arxiv_id": paper["arxiv_id"], "error": str(error)}]
            with connection:
                connection.execute("UPDATE digest_queue SET last_error=? WHERE arxiv_id=?", (str(error), paper["arxiv_id"]))
        return update

    def summarize(state):
        items = sorted(state["items"].values(), key=lambda item: (
            item["selection"]["bucket"] != "recsys", not bool(item["preferred_companies"]),
            -item["insight"]["analysis"]["rating"], -item["selection"]["score"]))[:state["limit"]]
        paper_window = {"field": "updated", "since_utc": state["since"],
                        "until_utc_exclusive": state["until"]} if state["since"] or state["until"] else None
        coverage = {"minimum": state["minimum"], "target": state["limit"], "actual": len(items),
                    "shortfall": max(0, state["minimum"] - len(items))}
        if coverage["shortfall"]:
            LOG.warning("日报数量未达标 date=%s actual=%s minimum=%s shortfall=%s search_rounds=%s paper_window=%s",
                        state["day"], len(items), state["minimum"], coverage["shortfall"], len(state["collections"]), paper_window)
        errors = list(state["errors"])
        synthesis_ok = True
        try:
            synthesis = synthesize(items, client, cache)
        except (ValueError, RuntimeError) as error:
            LOG.exception("日报综合失败 date=%s，单篇分析缓存保留供重试", state["day"])
            synthesis, synthesis_ok = {"trends": [], "topic": None}, False
            errors.append({"arxiv_id": "daily_synthesis", "error": str(error)})
        status = "partial" if errors else ("empty" if not items else "insufficient" if coverage["shortfall"] else "completed")
        payload = {"date": state["day"], "generated_at": iso(utcnow()), "status": status, "task_id": task_id,
                   "paper_window": paper_window, "coverage": coverage,
                   "discovery": state["previous_discovery"] + state["collections"],
                   "metadata_recovery": state.get("metadata_recovery", {}),
                   "provider": client.provider, "model": client.model, "items": items, "synthesis": synthesis,
                   "errors": errors, "rejected": state["rejected"]}
        return {"payload": payload, "synthesis_ok": synthesis_ok}

    def publish(state):
        from .publishing import publish_daily
        payload = state["payload"]
        current = connection.execute("SELECT payload_json FROM editions WHERE day=?", (state["day"],)).fetchone()
        revision = hashlib.sha256(current[0].encode()).hexdigest() if current else None
        if revision != state["edition_revision"] and (not current or json.loads(current[0]).get("task_id") != task_id):
            raise ValueError("该日报已被其他任务更新，保留最新版本；请重新运行 daily 补齐，而不是恢复旧任务。")
        other_ids = {item["paper"]["arxiv_id"]
                     for row in connection.execute("SELECT payload_json FROM editions WHERE day<>?", (state["day"],))
                     for item in json.loads(row[0]).get("items", [])}
        conflicts = other_ids & state["items"].keys()
        if conflicts:
            LOG.info("恢复期间其他日报已收录论文，重新补齐 date=%s arxiv_ids=%s", state["day"], sorted(conflicts))
            return {"items": {key: item for key, item in state["items"].items() if key not in other_ids},
                    "other_ids": sorted(other_ids), "result": None}
        with connection:
            connection.execute("INSERT OR REPLACE INTO editions VALUES (?,?)", (state["day"], dumps(payload)))
            if state["synthesis_ok"]:
                for item in payload["items"]:
                    paper = item["paper"]
                    connection.execute("INSERT OR REPLACE INTO digest_processed VALUES (?,?,?,?)",
                        (paper["arxiv_id"], paper["version"], "published", iso(utcnow())))
                    connection.execute("DELETE FROM digest_queue WHERE arxiv_id=?", (paper["arxiv_id"],))
            paths = publish_daily(payload, args.output_dir, connection)
        LOG.info("日报输出完成 date=%s status=%s papers=%s errors=%s paths=%s", state["day"], payload["status"],
                 len(payload["items"]), len(payload["errors"]), paths)
        since = timestamp(state["since"]) if state["since"] else None
        until = timestamp(state["until"]) if state["until"] else None
        return {"result": {"date": state["day"], "status": payload["status"], "fulltext_insights": len(payload["items"]),
                "webpage_ready": payload["status"] == "completed", "coverage": payload["coverage"],
                "discovery": state["collections"], "errors": payload["errors"],
                "metadata_recovery": state.get("metadata_recovery", {}),
                "pending": connection.execute("SELECT COUNT(*) FROM digest_queue").fetchone()[0],
                "pending_in_window": sum(in_window(job["paper"], since, until) for job in queued(connection)),
                "paper_window": payload["paper_window"], **paths}}

    builder = StateGraph(DailyState)
    builder.add_node("initialize", lambda state: _initial_state(args, config, connection, client))
    builder.add_node("recover_metadata", recover_metadata)
    builder.add_node("load_queue", load_queue)
    builder.add_node("expand_window", expand_window)
    builder.add_node("discover", search)
    builder.add_node("read_paper", read_paper)
    builder.add_node("synthesize", summarize)
    builder.add_node("publish", publish)
    builder.add_edge(START, "initialize")
    builder.add_edge("initialize", "recover_metadata")
    builder.add_edge("recover_metadata", "load_queue")
    builder.add_conditional_edges("load_queue", next_step)
    builder.add_conditional_edges("read_paper", next_step)
    builder.add_edge("expand_window", "load_queue")
    builder.add_edge("discover", "load_queue")
    builder.add_edge("synthesize", "publish")
    builder.add_conditional_edges("publish", lambda state: END if state.get("result") else "load_queue")
    request = {"args": {key: str(value.resolve()) if isinstance(value, Path) else value for key, value in vars(args).items()},
               "config": config}
    task_id, state = execute_graph(builder, {"task_id": task_id}, directory=args.db.parent,
                                  kind=getattr(args, "command", "digest"), request=request, task_id=task_id,
                                  resume=getattr(args, "resume", False))
    return {**state["result"], "task_id": task_id}
