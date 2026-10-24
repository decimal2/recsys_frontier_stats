"""Check persisted diagnostics, isolation between invocations, and credential redaction."""
import io
import json
import logging
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from paper_pipeline.cli import main
from paper_pipeline.run_logging import BEIJING, run_logging


class LoggingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("paper_pipeline.cli.LOG_ROOT", self.directory / "logs"), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--db", str(self.directory / "papers.sqlite3"), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_separate_runs_keep_success_result_and_failure_traceback(self):
        code, output, stderr = self.invoke("pending")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["pending"], [])
        first_path = next((self.directory / "logs").glob("*/*.log"))
        first_log = first_path.read_text()
        self.assertIn("[INFO]", first_log)
        self.assertIn("运行结果", first_log)
        self.assertIn("exit_code=0", first_log)
        self.assertIn(str(first_path), stderr)

        code, output, stderr = self.invoke("resume", "no-such-task")
        self.assertEqual(code, 1)
        paths = list((self.directory / "logs").glob("*/*.log"))
        self.assertEqual(len(paths), 2)
        second_log = next(p for p in paths if p != first_path).read_text()
        self.assertEqual(first_path.read_text(), first_log)
        self.assertIn("[ERROR]", second_log)
        self.assertIn("Traceback (most recent call last)", second_log)
        self.assertIn("ValueError: 找不到任务", second_log)
        self.assertIn("exit_code=1", second_log)
        self.assertEqual(second_log.count("运行开始 pid="), 1)

    def test_warning_exception_and_console_are_redacted(self):
        secret = "fake-sensitive-key-for-log-test"
        stderr = io.StringIO()
        logger = logging.getLogger("paper_pipeline.daily")
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": secret}), redirect_stderr(stderr):
            with run_logging(self.directory / "logs") as path:
                logger.warning("重试 arxiv_id=2305.05065 Authorization: Bearer %s", secret)
                try:
                    raise RuntimeError("failure " + secret)
                except RuntimeError:
                    logger.exception("论文处理失败 arxiv_id=2305.05065")
        for text in (path.read_text(), stderr.getvalue()):
            self.assertIn("[WARNING]", text)
            self.assertIn("[ERROR]", text)
            self.assertIn("arxiv_id=2305.05065", text)
            self.assertIn("Traceback (most recent call last)", text)
            self.assertIn("[REDACTED]", text)
            self.assertNotIn(secret, text)

    def test_bad_arguments_are_persisted(self):
        code, _, _ = self.invoke("not-a-command")
        self.assertEqual(code, 2)
        text = next((self.directory / "logs").glob("*/*.log")).read_text()
        self.assertIn("参数错误", text)
        self.assertIn("not-a-command", text)
        self.assertIn("exit_code=2", text)

    def test_unexpected_exception_is_persisted(self):
        with patch("paper_pipeline.cli.load_config", side_effect=AssertionError("unexpected-test-failure")):
            code, _, _ = self.invoke("pending")
        self.assertEqual(code, 1)
        text = next((self.directory / "logs").glob("*/*.log")).read_text()
        self.assertIn("未处理的异常", text)
        self.assertIn("AssertionError: unexpected-test-failure", text)
        self.assertIn("Traceback (most recent call last)", text)

    def test_date_directory_and_filename_follow_beijing_start_time(self):
        moments = [datetime(2026, 10, 23, 23, 59, 59, tzinfo=BEIJING),
                   datetime(2026, 10, 24, 0, 0, 1, tzinfo=BEIJING)]
        for moment in moments:
            with self.subTest(moment=moment), redirect_stderr(io.StringIO()), \
                 patch("paper_pipeline.run_logging.datetime", wraps=datetime) as clock:
                clock.now.return_value = moment
                with run_logging(self.directory / "logs") as path:
                    logging.getLogger("paper_pipeline").info("date test")
                clock.now.assert_called_once_with(BEIJING)
                self.assertEqual(path.relative_to(self.directory / "logs"),
                                 Path(moment.strftime("%Y-%m-%d/%H-%M-%S.log")))
                self.assertIn("date test", path.read_text())

    def test_same_second_runs_do_not_overwrite_previous_log(self):
        logger = logging.getLogger("paper_pipeline")
        paths = []
        with redirect_stderr(io.StringIO()), patch("paper_pipeline.run_logging.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 10, 24, 9, 8, 7, tzinfo=BEIJING)
            for index in range(3):
                with run_logging(self.directory / "logs") as path:
                    logger.info("invocation=%s", index)
                    paths.append(path)
        self.assertEqual([path.name for path in paths], ["09-08-07.log", "09-08-07-1.log", "09-08-07-2.log"])
        for index, path in enumerate(paths):
            contents = path.read_text()
            self.assertEqual(contents.count("invocation="), 1)
            self.assertIn(f"invocation={index}", contents)


if __name__ == "__main__":
    unittest.main()
