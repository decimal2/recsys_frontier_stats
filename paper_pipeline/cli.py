import argparse
import fcntl
import hashlib
import json
import importlib.util
import logging
import os
import sqlite3
import sys
import time as clock
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from .arxiv import iso, utcnow
from .providers import OFFICIAL, OfficialClient, DeepSeekSearch, load_env
from .run_logging import run_logging
from .storage import connect

ROOT = Path(__file__).resolve().parents[1]
LOG_ROOT = ROOT / "logs"
LOG = logging.getLogger(__name__)


class LoggedArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        LOG.error("参数错误：%s", message)
        super().error(message)


def load_config(path):
    config = json.loads(path.read_text(encoding="utf-8"))
    arxiv = config["arxiv"]
    if type(arxiv["timeout_seconds"]) is not int or arxiv["timeout_seconds"] < 1:
        raise ValueError("arxiv.timeout_seconds must be a positive integer")
    if type(arxiv["retries"]) is not int or not 0 <= arxiv["retries"] <= 10:
        raise ValueError("arxiv.retries must be between 0 and 10")
    if not 3 <= arxiv["request_interval_seconds"] <= 3600:
        raise ValueError("arXiv request_interval_seconds must be >= 3 and <= 3600")
    bounds = {"max_rounds": (3, 50), "max_tool_calls": (2, 100),
              "max_candidates": (1, 100), "max_page_chars": (1000, 100000)}
    for key, (low, high) in bounds.items():
        value = config["discovery"][key]
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"discovery.{key} must be {low}..{high}")
    if type(config["search"]["count"]) is not int or not 1 <= config["search"]["count"] <= 50:
        raise ValueError("search.count must be 1..50")
    if not isinstance(arxiv["user_agent"], str) or not arxiv["user_agent"].strip():
        raise ValueError("arxiv.user_agent is required")
    if not isinstance(config["companies"], list) or any(not isinstance(name, str) or not name.strip() for name in config["companies"]):
        raise ValueError("companies must be an array of names")
    if not isinstance(config["preferred_companies"], list) or any(name not in config["companies"] for name in config["preferred_companies"]):
        raise ValueError("preferred_companies must use canonical names from companies")
    aliases = ROOT / "company_aliases.json"
    config["company_aliases"] = {**(json.loads(aliases.read_text(encoding="utf-8")) if aliases.exists() else {}),
                                 **config.get("company_aliases", {})}
    if config["llm"]["provider"] not in OFFICIAL:
        raise ValueError("llm.provider must be deepseek, kimi, or glm")
    for provider in OFFICIAL:
        profile = config["llm"]["providers"][provider]
        if not isinstance(profile.get("model"), str) or not profile["model"].strip():
            raise ValueError("Missing model for " + provider)
        if not isinstance(profile.get("parameters", {}), dict):
            raise ValueError("Invalid model parameters")
        if set(profile.get("parameters", {})) - {"thinking", "reasoning_effort", "temperature", "top_p"}:
            raise ValueError("Only generation parameters may be overridden")
    for key in ("timeout_seconds", "max_tokens"):
        if type(config["llm"][key]) is not int or config["llm"][key] < 1:
            raise ValueError("Invalid llm." + key)
    if type(config["llm"]["retries"]) is not int or not 0 <= config["llm"]["retries"] <= 5:
        raise ValueError("llm.retries must be 0..5")
    for key in ("max_papers", "max_fulltext_chars"):
        if type(config["digest"][key]) is not int or config["digest"][key] < 1:
            raise ValueError("Invalid digest." + key)
    minimum = config["digest"]["min_papers"]
    if type(minimum) is not int or not 1 <= minimum <= config["digest"]["max_papers"]:
        raise ValueError("digest.min_papers must be 1..digest.max_papers")
    windows = config["digest"]["lookback_windows_days"]
    if (not isinstance(windows, list) or not windows
            or any(type(days) is not int or not 1 <= days <= 365 for days in windows)
            or windows != sorted(set(windows))):
        raise ValueError("digest.lookback_windows_days must be increasing unique days within 1..365")
    return config


@contextmanager
def database_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Another pipeline process is using this database") from error
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def parser():
    result = LoggedArgumentParser(description="推荐系统论文发现、全文 insight 和中文日报（DeepSeek / Kimi / GLM 官方 API）")
    result.add_argument("--config", type=Path, default=ROOT / "pipeline_config.json")
    result.add_argument("--db", type=Path, default=ROOT / "data" / "papers.sqlite3")
    result.add_argument("--output-dir", type=Path, help="默认 reports/；自定义数据库时默认其目录下的 reports/")
    commands = result.add_subparsers(dest="command", required=True)
    for name, help_text in (("digest", "处理待解读队列并生成中文日报"), ("daily", "采集 + 全文解读 + 日报，一条命令运行")):
        digest = commands.add_parser(name, help=help_text)
        digest.add_argument("--provider", choices=list(OFFICIAL))
        digest.add_argument("--date", help="日报归档日期；daily 未指定 --until 时也作为截止日期")
        digest.add_argument("--limit", type=int, help="本期论文总数上限，默认 10，不得低于 digest.min_papers（默认 8）")
        digest.add_argument("--run-id", type=int, help="将指定运行的候选加入待解读队列")
        digest.add_argument("--since", help="起始日期（UTC）；daily 作为初始线索，至少搜索近 7 天并按需前扩；digest 严格限定")
        digest.add_argument("--until", help="论文更新时间终点（UTC）；日期包含当天，ISO 时间为右开边界")
    fulltext = commands.add_parser("fulltext", help="下载或导入一篇论文的全文，生成可引用文本块")
    fulltext.add_argument("--id", required=True, help="arXiv ID，可带版本")
    fulltext.add_argument("--version", type=int)
    fulltext.add_argument("--file", type=Path, help="本地 PDF 或 arXiv HTML；省略则下载")
    discovery = commands.add_parser("discover", help="模型自主调用 web_search / web_fetch 发现论文并入库")
    discovery.add_argument("--provider", choices=list(OFFICIAL))
    discovery.add_argument("--since", help="论文更新时间起点（UTC），默认最近 7 天")
    discovery.add_argument("--until", help="论文更新时间终点（UTC），日期包含当天")
    fetch = commands.add_parser("fetch", help="用 DeepSeek 官方 Fetch 保存网页 Markdown 证据")
    fetch.add_argument("--url", required=True)
    commands.add_parser("doctor", help="检查配置和依赖，不联网、不显示密钥")
    commands.add_parser("pending", help="查看全文解读待处理队列")
    commands.add_parser("preview", help="用已有解读重新生成日报及归档，不联网、不调用模型")
    tasks = commands.add_parser("tasks", help="查看 LangGraph 任务状态，可在任务运行时使用")
    tasks.add_argument("--task-id", help="只查看指定任务")
    resume = commands.add_parser("resume", help="按已保存配置恢复中断的任务")
    resume.add_argument("task_id")
    return result


def _main(argv=None):
    load_env(ROOT / ".env")
    args = parser().parse_args(argv)
    if args.output_dir is None:
        args.output_dir = ROOT / "reports" if args.db == ROOT / "data" / "papers.sqlite3" else args.db.parent / "reports"
    LOG.info("命令=%s 数据库=%s 配置=%s 输出目录=%s", args.command, args.db, args.config, args.output_dir)
    try:
        if args.command == "tasks":
            from .runtime import get_task, list_tasks
            tasks = [get_task(args.db.parent, args.task_id)] if args.task_id else list_tasks(args.db.parent)
            print(json.dumps({"tasks": [task for task in tasks if task]}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "resume":
            from .runtime import get_task
            task = get_task(args.db.parent, args.task_id)
            if task is None:
                raise ValueError("找不到任务：" + args.task_id)
            request = task["request"]
            args = argparse.Namespace(**request["args"])
            for key in ("db", "config", "output_dir"):
                if getattr(args, key, None) is not None:
                    setattr(args, key, Path(getattr(args, key)))
            args.command, args.task_id, args.resume = task["kind"], task["task_id"], True
            config = request["config"]
        else:
            config = load_config(args.config)
        with database_lock(args.db):
            connection = connect(args.db)
            try:
                # The lock guarantees these rows belong to processes that already exited.
                with connection:
                    connection.execute("UPDATE runs SET status='failed',finished_at=?,error=? WHERE status='running'",
                                       (iso(utcnow()), "Previous process interrupted; checkpoint unchanged"))
                if args.command == "doctor":
                    output = {"providers": {name: {"model": config["llm"]["providers"][name]["model"],
                        "endpoint": endpoint, "key_env": env, "key_configured": bool(os.environ.get(env))}
                        for name, (endpoint, env) in OFFICIAL.items()},
                        "dependencies": {module: importlib.util.find_spec(module) is not None for module in ("bs4", "fitz", "langgraph")},
                        "database": str(args.db), "network_tested": False}
                elif args.command == "pending":
                    from .daily import queued
                    output = {"pending": [{"arxiv_id": job["paper"]["arxiv_id"], "title": job["paper"]["title"],
                        "score": job["selection"]["score"], "last_error": job["last_error"]} for job in queued(connection)]}
                elif args.command == "preview":
                    from .publishing import rebuild_reports
                    output = rebuild_reports(connection, args.output_dir, config)
                elif args.command == "fulltext":
                    from .arxiv import arxiv_id
                    from .fulltext import FullTextStore
                    identifier, version = arxiv_id(args.id)
                    if args.version is not None:
                        if args.version < 0:
                            raise ValueError("Version must be >= 0")
                        version = args.version
                    store = FullTextStore(config, args.db.parent / "fulltext")
                    document = store.import_file(identifier, version, args.file) if args.file else store.get({"arxiv_id": identifier, "version": version})
                    output = {"arxiv_id": identifier, "version": version, "blocks": len(document["blocks"]),
                              "characters": document["content_chars"], "format": document["format"],
                              "cache": str(store.location(identifier, version) / "document.json"), "note": document["note"]}
                elif args.command == "fetch":
                    url = urllib.parse.urlparse(args.url)
                    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
                        raise ValueError("Fetch requires an HTTP(S) URL without credentials")
                    content = DeepSeekSearch(config).fetch(args.url)
                    target = args.db.parent / "web" / (hashlib.sha256(args.url.encode()).hexdigest() + ".json")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    from .publishing import atomic_write
                    atomic_write(target, json.dumps({"url": args.url, "fetched_at": iso(utcnow()), **content}, ensure_ascii=False, indent=2))
                    output = {"saved": str(target), "characters": len(content["content"])}
                elif args.command == "discover":
                    from .discovery import discover
                    output = discover(args, config, connection)
                elif args.command in {"digest", "daily"}:
                    from .daily import digest
                    if args.date:
                        date.fromisoformat(args.date)
                    elif args.since and args.since == args.until and len(args.since) == 10:
                        args.date = date.fromisoformat(args.since).isoformat()
                    elif args.command == "daily" and args.until and len(args.until) == 10:
                        args.date = date.fromisoformat(args.until).isoformat()
                    if args.limit is not None and args.limit < config["digest"]["min_papers"]:
                        raise ValueError(f"--limit must be >= digest.min_papers ({config['digest']['min_papers']})")
                    client = OfficialClient(config, args.provider)
                    client.validate_generation()
                    client.require_key()
                    output = digest(args, config, connection)
                else:
                    raise ValueError("未知命令：" + args.command)
                LOG.info("运行结果：%s", json.dumps(output, ensure_ascii=False))
                print(json.dumps(output, ensure_ascii=False, indent=2))
                if args.command in {"daily", "digest"}:
                    webpage = Path(output["html"]).resolve()
                    if output["webpage_ready"]:
                        LOG.info("日报网页已生成：%s（%s 篇全文解读，含综合总结；可直接用浏览器打开）",
                                 webpage, output["fulltext_insights"])
                    elif output["fulltext_insights"]:
                        LOG.warning("日报尚未全部完成：%s/%s 篇，缺口 %s 篇；已完成内容可查看：%s；详情见日志与 coverage",
                                    output["fulltext_insights"], output["coverage"]["minimum"], output["coverage"]["shortfall"], webpage)
                    else:
                        LOG.warning("尚未生成论文总结；本次说明页：%s；请查看检索结果和错误日志", webpage)
            finally:
                connection.close()
        return 1 if output.get("status") in {"partial", "empty", "insufficient", "budget_exhausted"} else 0
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, ImportError, sqlite3.Error, ET.ParseError,
            urllib.error.URLError) as error:
        LOG.exception("运行失败：%s", error)
        return 1


def main(argv=None):
    with run_logging(LOG_ROOT) as log_path:
        started = clock.monotonic()
        exit_code = 1
        LOG.info("运行开始 pid=%s 日志=%s", os.getpid(), log_path)
        try:
            exit_code = _main(argv)
        except KeyboardInterrupt:
            LOG.warning("运行被用户中断", exc_info=True)
            exit_code = 130
        except SystemExit as error:
            exit_code = error.code if isinstance(error.code, int) else 1
            if exit_code:
                LOG.error("命令行解析退出 exit_code=%s", exit_code, exc_info=True)
        except Exception:
            LOG.exception("未处理的异常导致运行失败")
        finally:
            LOG.log(logging.INFO if exit_code == 0 else logging.ERROR,
                    "运行结束 exit_code=%s elapsed_seconds=%.3f", exit_code, clock.monotonic() - started)
        return exit_code
