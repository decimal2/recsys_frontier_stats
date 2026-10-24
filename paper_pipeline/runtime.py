"""Run local LangGraph tasks with durable checkpoints and readable task metadata."""
import json
import logging
import time
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver

from . import storage
from .arxiv import iso, utcnow
from .providers import redact_secrets

LOG = logging.getLogger(__name__)
DATABASE = "agent_checkpoints.sqlite3"  # LangGraph checkpoints only
MAIN_DATABASE = "papers.sqlite3"  # agent_tasks lives here


def _connect(directory):
    return storage.connect(Path(directory) / MAIN_DATABASE)


def _task(row):
    if row is None:
        return None
    result = dict(row)
    result["request"] = json.loads(result.pop("request_json"))
    payload = result.pop("result_json")
    result["result"] = json.loads(payload) if payload is not None else None
    return result


def _has_databases(directory):
    directory = Path(directory)
    return (directory / MAIN_DATABASE).exists() or (directory / DATABASE).exists()


def get_task(directory, task_id):
    if not _has_databases(directory):
        return None
    with closing(_connect(directory)) as connection:
        return _task(connection.execute("SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone())


def list_tasks(directory):
    if not _has_databases(directory):
        return []
    with closing(_connect(directory)) as connection:
        return [_task(row) for row in connection.execute("SELECT * FROM agent_tasks ORDER BY created_at DESC, task_id")]


def execute_graph(builder, initial, *, directory, kind, request, task_id=None, resume=False, connection=None):
    """Return (task_id, state); callers keep credentials and reasoning out of state.

    Graph nodes run sequentially. Persisted inputs and outputs must be serializable;
    external writes remain the node's responsibility to make safe to repeat.
    Task metadata goes to the main database; pass its connection if already open.
    """
    if connection is None:
        with closing(_connect(directory)) as owned:
            return execute_graph(builder, initial, directory=directory, kind=kind, request=request,
                                 task_id=task_id, resume=resume, connection=owned)
    if resume and not task_id:
        raise ValueError("Resuming a task requires task_id")
    task_id = task_id or uuid4().hex
    path = Path(directory) / DATABASE
    previous = _task(connection.execute("SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone())
    if resume:
        if previous is None:
            raise ValueError(f"Unknown task: {task_id}")
        if previous["kind"] != kind:
            raise ValueError(f"Task {task_id} is {previous['kind']}, not {kind}")
    elif previous is not None:
        raise ValueError(f"Task already exists: {task_id}; resume it or use a new task_id")
    else:
        now = iso(utcnow())
        with connection:
            connection.execute("""INSERT INTO agent_tasks
                               (task_id,kind,request_json,status,created_at,updated_at)
                               VALUES (?,?,?,'running',?,?)""",
                               (task_id, kind, json.dumps(request, ensure_ascii=False), now, now))
    with connection:
        connection.execute("UPDATE agent_tasks SET status='running',error=NULL,updated_at=? WHERE task_id=?",
                           (iso(utcnow()), task_id))
    started = time.monotonic()
    LOG.info("Agent 任务开始 task_id=%s kind=%s resume=%s", task_id, kind, resume)
    try:
        with SqliteSaver.from_conn_string(str(path)) as checkpointer:
            graph = builder.compile(checkpointer=checkpointer)
            run_config = {"configurable": {"thread_id": task_id}, "recursion_limit": 1000,
                          "max_concurrency": 1}
            saved = graph.get_state(run_config) if resume and checkpointer.get_tuple(run_config) else None
            # A crash can happen after the final checkpoint but before task metadata is committed.
            if saved is None or saved.next:
                graph_input = None if saved is not None else initial
                node_started = {}
                for event in graph.stream(graph_input, run_config, stream_mode="tasks", durability="sync"):
                    name = event["name"]
                    if "input" in event:
                        node_started[event["id"]] = time.monotonic()
                        with connection:
                            connection.execute("UPDATE agent_tasks SET last_node=?,updated_at=? WHERE task_id=?",
                                               (name, iso(utcnow()), task_id))
                        LOG.info("Agent 节点开始 task_id=%s node=%s", task_id, name)
                    else:
                        elapsed = time.monotonic() - node_started.pop(event["id"], started)
                        LOG.info("Agent 节点结束 task_id=%s node=%s elapsed_seconds=%.3f failed=%s",
                                 task_id, name, elapsed, bool(event.get("error")))
            state = dict(graph.get_state(run_config).values)
        with connection:
            result = state.get("output", state.get("result"))
            connection.execute("UPDATE agent_tasks SET status='completed',result_json=?,updated_at=? WHERE task_id=?",
                               (json.dumps(result, ensure_ascii=False), iso(utcnow()), task_id))
        LOG.info("Agent 任务完成 task_id=%s elapsed_seconds=%.3f", task_id, time.monotonic() - started)
        return task_id, state
    except (Exception, KeyboardInterrupt) as error:
        with connection:
            connection.execute("UPDATE agent_tasks SET status='failed',error=?,updated_at=? WHERE task_id=?",
                               (redact_secrets(str(error)), iso(utcnow()), task_id))
        LOG.exception("Agent 任务失败 task_id=%s，可使用 resume 恢复", task_id)
        raise
