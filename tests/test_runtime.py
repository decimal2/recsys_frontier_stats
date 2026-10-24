"""Exercise real LangGraph SQLite persistence across process termination."""
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import TypedDict
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from langgraph.graph import END, START, StateGraph
from paper_pipeline.runtime import DATABASE, MAIN_DATABASE, execute_graph, get_task, list_tasks


class CounterState(TypedDict):
    value: int


def counter_graph(callback):
    graph = StateGraph(CounterState)
    graph.add_node("count", callback)
    graph.add_edge(START, "count")
    graph.add_edge("count", END)
    return graph


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def test_killed_process_resumes_without_repeating_completed_node(self):
        worker = self.directory / "worker.py"
        worker.write_text(textwrap.dedent(f"""
            import os, sys
            from pathlib import Path
            from typing import TypedDict
            sys.path.insert(0, {str(ROOT)!r})
            from langgraph.graph import StateGraph, START, END
            from paper_pipeline.runtime import execute_graph

            directory = Path(sys.argv[1])
            resume = sys.argv[2] == 'resume'
            class State(TypedDict):
                value: int
            def record(name):
                with (directory / 'events').open('a') as file:
                    file.write(name + '\\n')
            def first(state):
                record('first')
                return {{'value': state['value'] + 1}}
            def second(state):
                record('second')
                if not resume:
                    os._exit(37)
                return {{'value': state['value'] + 1}}
            builder = StateGraph(State)
            builder.add_node('first', first)
            builder.add_node('second', second)
            builder.add_edge(START, 'first')
            builder.add_edge('first', 'second')
            builder.add_edge('second', END)
            _, state = execute_graph(builder, {{'value': 0}}, directory=directory,
                                     kind='test', request={{'topic': 'resume'}},
                                     task_id='restart-test', resume=resume)
            assert state['value'] == 2, state
        """), encoding="utf-8")
        first = subprocess.run([sys.executable, str(worker), str(self.directory), "start"],
                               capture_output=True, text=True, timeout=20)
        self.assertEqual(first.returncode, 37, first.stderr)
        task = get_task(self.directory, "restart-test")
        self.assertEqual(task["status"], "running")
        self.assertEqual(task["last_node"], "second")
        second = subprocess.run([sys.executable, str(worker), str(self.directory), "resume"],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual((self.directory / "events").read_text().splitlines(), ["first", "second", "second"])
        self.assertEqual(get_task(self.directory, "restart-test")["status"], "completed")

    def test_final_checkpoint_wins_over_stale_task_status(self):
        calls = []
        builder = counter_graph(lambda state: calls.append("count") or {"value": state["value"] + 1})
        task_id, result = execute_graph(builder, {"value": 2}, directory=self.directory,
                                        kind="test", request={"date": "2026-10-22"})
        self.assertEqual(result["value"], 3)
        with sqlite3.connect(self.directory / MAIN_DATABASE) as connection:
            connection.execute("UPDATE agent_tasks SET status='running' WHERE task_id=?", (task_id,))
        _, resumed = execute_graph(builder, {"value": 99}, directory=self.directory,
                                   kind="test", request={}, task_id=task_id, resume=True)
        self.assertEqual(resumed["value"], 3)
        self.assertEqual(calls, ["count"])
        self.assertEqual(list_tasks(self.directory)[0]["request"], {"date": "2026-10-22"})
        with self.assertRaisesRegex(ValueError, "already exists"):
            execute_graph(builder, {"value": 0}, directory=self.directory,
                          kind="test", request={}, task_id=task_id)

    def test_failed_node_retains_redacted_error_and_can_retry(self):
        attempts = []
        secret = "runtime-test-secret"
        def step(state):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("Failure " + secret)
            return {"value": state["value"] + 1}
        builder = counter_graph(step)
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": secret}), self.assertLogs("paper_pipeline.runtime"):
            with self.assertRaises(RuntimeError):
                execute_graph(builder, {"value": 1}, directory=self.directory,
                              kind="test", request={}, task_id="retry")
        task = get_task(self.directory, "retry")
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["last_node"], "count")
        self.assertNotIn(secret, task["error"])
        self.assertIn("[REDACTED]", task["error"])
        _, result = execute_graph(builder, {"value": 100}, directory=self.directory,
                                  kind="test", request={}, task_id="retry", resume=True)
        self.assertEqual(result["value"], 2)
        self.assertIsNone(get_task(self.directory, "retry")["error"])

    def test_nested_tasks_share_database_without_reusing_parent_thread(self):
        child = counter_graph(lambda state: {"value": state["value"] + 1})
        def run_child(state):
            _, result = execute_graph(child, state, directory=self.directory,
                                      kind="child", request={}, task_id="child")
            return result
        parent = counter_graph(run_child)
        _, result = execute_graph(parent, {"value": 4}, directory=self.directory,
                                  kind="parent", request={}, task_id="parent")
        self.assertEqual(result["value"], 5)
        self.assertEqual({task["task_id"] for task in list_tasks(self.directory)}, {"child", "parent"})
        self.assertTrue(all(task["status"] == "completed" for task in list_tasks(self.directory)))

    def test_missing_task_lookup_is_read_only_and_resume_is_explicit(self):
        self.assertIsNone(get_task(self.directory, "missing"))
        self.assertEqual(list_tasks(self.directory), [])
        self.assertFalse((self.directory / DATABASE).exists())
        self.assertFalse((self.directory / MAIN_DATABASE).exists())
        with self.assertRaisesRegex(ValueError, "Unknown task"):
            execute_graph(counter_graph(lambda state: state), {"value": 0}, directory=self.directory,
                          kind="test", request={}, task_id="missing", resume=True)

    def test_resume_before_first_checkpoint_uses_original_input(self):
        builder = counter_graph(lambda state: {"value": state["value"] + 1})
        with patch.object(builder, "compile", side_effect=RuntimeError("startup failed")), \
                self.assertLogs("paper_pipeline.runtime"), self.assertRaises(RuntimeError):
            execute_graph(builder, {"value": 2}, directory=self.directory,
                          kind="test", request={}, task_id="startup")
        _, result = execute_graph(builder, {"value": 2}, directory=self.directory,
                                  kind="test", request={}, task_id="startup", resume=True)
        self.assertEqual(result["value"], 3)

    def test_sequential_nodes_can_use_callers_sqlite_connection(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE numbers (value INTEGER)")
        def step(state):
            connection.execute("INSERT INTO numbers VALUES (?)", (state["value"],))
            return state
        execute_graph(counter_graph(step), {"value": 7}, directory=self.directory,
                      kind="test", request={})
        self.assertEqual(connection.execute("SELECT value FROM numbers").fetchone()[0], 7)

    def test_result_preserves_business_status_after_reopening_and_schema_upgrade(self):
        with sqlite3.connect(self.directory / DATABASE) as connection:
            connection.execute("""CREATE TABLE agent_tasks (
                task_id TEXT PRIMARY KEY, kind TEXT NOT NULL, request_json TEXT NOT NULL,
                status TEXT NOT NULL, last_node TEXT, error TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        class ResultState(TypedDict):
            result: dict
        builder = StateGraph(ResultState)
        builder.add_node("publish", lambda state: {"result": {"status": "partial", "answer": "已完成部分资料"}})
        builder.add_edge(START, "publish")
        builder.add_edge("publish", END)
        execute_graph(builder, {"result": {}}, directory=self.directory,
                      kind="agent", request={}, task_id="partial")
        saved = get_task(self.directory, "partial")
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["result"], {"status": "partial", "answer": "已完成部分资料"})
        self.assertEqual(list_tasks(self.directory)[0]["result"]["status"], "partial")

    def test_legacy_checkpoint_tasks_migrate_once_into_main_database(self):
        with sqlite3.connect(self.directory / DATABASE) as legacy:
            legacy.execute("""CREATE TABLE agent_tasks (
                task_id TEXT PRIMARY KEY, kind TEXT NOT NULL, request_json TEXT NOT NULL,
                status TEXT NOT NULL, last_node TEXT, error TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            legacy.execute("INSERT INTO agent_tasks VALUES ('old','agent','{}','completed','final',NULL,"
                           "'2026-10-01T00:00:00+00:00','2026-10-01T00:00:00+00:00')")
        task = get_task(self.directory, "old")
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["last_node"], "final")
        self.assertIsNone(task["result"])
        # A second lookup re-runs the migration without duplicating or failing.
        self.assertEqual([task["task_id"] for task in list_tasks(self.directory)], ["old"])
        with sqlite3.connect(self.directory / DATABASE) as legacy:
            legacy.execute("UPDATE agent_tasks SET status='failed' WHERE task_id='old'")
        self.assertEqual(get_task(self.directory, "old")["status"], "completed")


if __name__ == "__main__":
    unittest.main()
