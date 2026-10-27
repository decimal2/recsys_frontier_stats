"""Exercise shell entry points with real local Git repositories; no API calls."""
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


class ShellScriptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.project = self.directory / "project with spaces"
        shutil.copytree(ROOT / "scripts", self.project / "scripts")
        (self.project / ".venv/bin").mkdir(parents=True)
        (self.project / ".venv/bin/python").symlink_to(sys.executable)
        self.env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_AUTHOR_NAME": "Script Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                    "GIT_COMMITTER_NAME": "Script Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}
        self.env.pop("PUBLISH_BRANCH", None)
        self.env.pop("PUBLISH_VERIFY_TIMEOUT", None)
        self.env["PUBLISH_SITE_URL"] = "https://example.invalid/recsys_frontier_stats/"

    def git(self, directory, *args):
        return subprocess.check_output(["git", "-C", str(directory), *args], env=self.env,
                                       stderr=subprocess.STDOUT, text=True).strip()

    def run_script(self, name, *args):
        return subprocess.run(["bash", str(self.project / "scripts" / name), *args], cwd=self.directory,
                              env=self.env, text=True, capture_output=True, timeout=30)

    def seed_publishing(self):
        seed = self.directory / "seed"
        (seed / "docs").mkdir(parents=True)
        (seed / "docs/index.html").write_text("<h1>Old homepage</h1>")
        (seed / "docs/kept.html").write_text("<h1>Existing paper</h1>")
        (seed / "README.md").write_text("Keep remote code unchanged")
        self.git(seed, "init", "-b", "main")
        self.git(seed, "add", ".")
        self.git(seed, "commit", "-m", "seed")
        self.remote = self.directory / "remote.git"
        subprocess.run(["git", "clone", "--bare", str(seed), str(self.remote)], env=self.env,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        self.env["PUBLISH_REPO_URL"] = str(self.remote)
        site = self.project / "reports/site"
        (site / "daily").mkdir(parents=True)
        (site / "index.html").write_text('<h1>新日报</h1><a href="daily/index.html">日报</a><a href="kept.html">论文</a>')
        (site / "daily/index.html").write_text('<a href="../index.html">首页</a>')
        self.git(self.project, "init", "-b", "main")
        (self.project / "README.md").write_text("Local work")
        self.git(self.project, "add", "README.md")
        self.git(self.project, "commit", "-m", "unrelated local history")
        (self.project / "README.md").write_text("Uncommitted local work")
        self.remote_before = self.git(self.remote, "rev-parse", "main")
        self.local_before = self.git(self.project, "rev-parse", "HEAD")

    def test_collect_forwards_arguments_and_exit_code_from_any_directory(self):
        (self.project / "pipeline.py").write_text(
            "import json, os, sys\nfrom pathlib import Path\n"
            "Path('invocation.json').write_text(json.dumps({'args': sys.argv[1:], 'cwd': os.getcwd()}))\n"
            "raise SystemExit(int(os.environ.get('COLLECT_TEST_EXIT', '0')))\n")
        result = self.run_script("collect_daily.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        first = json.loads((self.project / "invocation.json").read_text())
        self.assertEqual(first, {"args": ["daily"], "cwd": str(self.project)})
        self.env["COLLECT_TEST_EXIT"] = "1"
        args = ["--date", "2026-10-27", "--provider", "kimi", "--limit", "10"]
        result = self.run_script("collect_daily.sh", *args)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads((self.project / "invocation.json").read_text())["args"], ["daily", *args])

    def test_publish_changes_only_docs_preserves_other_pages_and_is_idempotent(self):
        self.seed_publishing()
        result = self.run_script("publish.sh", "--no-verify")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        head = self.git(self.remote, "rev-parse", "main")
        self.assertNotEqual(head, self.remote_before)
        changed = self.git(self.remote, "diff", "--name-only", self.remote_before, head).splitlines()
        self.assertEqual(set(changed), {"docs/index.html", "docs/daily/index.html", "docs/.nojekyll"})
        self.assertEqual(self.git(self.remote, "show", "main:docs/kept.html"), "<h1>Existing paper</h1>")
        self.assertEqual(self.git(self.remote, "show", "main:README.md"), "Keep remote code unchanged")
        self.assertEqual(self.git(self.project, "rev-parse", "HEAD"), self.local_before)
        self.assertEqual((self.project / "README.md").read_text(), "Uncommitted local work")
        self.assertFalse((self.project / "docs").exists())
        repeated = self.run_script("publish.sh", "--no-verify")
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertEqual(self.git(self.remote, "rev-parse", "main"), head)
        self.assertIn("无需新提交", repeated.stdout)

    def test_dry_run_validates_without_pushing_or_updating_local_docs(self):
        self.seed_publishing()
        result = self.run_script("publish.sh", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.remote, "rev-parse", "main"), self.remote_before)
        self.assertFalse((self.project / "docs").exists())
        self.assertIn("dry-run", result.stdout)

    def test_missing_links_and_nonstatic_files_prevent_push(self):
        self.seed_publishing()
        site = self.project / "reports/site"
        for name in ("paper.pdf", ".env", "run.log"):
            with self.subTest(name=name):
                unwanted = site / name
                unwanted.write_text("Do not publish")
                result = self.run_script("publish.sh", "--no-verify")
                unwanted.unlink()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("非静态发布文件", result.stdout)
                self.assertEqual(self.git(self.remote, "rev-parse", "main"), self.remote_before)
        (site / "index.html").write_text('<img src="missing.png">')
        result = self.run_script("publish.sh", "--no-verify")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing.png", result.stdout)
        self.assertEqual(self.git(self.remote, "rev-parse", "main"), self.remote_before)

    def test_push_rejection_is_nonzero_and_captured_in_log(self):
        self.seed_publishing()
        hook = self.remote / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\necho 'synthetic push rejection' >&2\nexit 1\n")
        hook.chmod(0o755)
        result = self.run_script("publish.sh", "--no-verify")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git(self.remote, "rev-parse", "main"), self.remote_before)
        logs = list((self.project / "logs").glob("????-??-??/*.log"))
        self.assertEqual(len(logs), 1)
        content = logs[0].read_text()
        self.assertIn("synthetic push rejection", content)
        self.assertIn("[ERROR]", content)
        self.assertIn("exit_code=1", content)

    def verification_code(self):
        # Run the actual embedded verifier with fake HTTP, not a parallel implementation.
        script = (ROOT / "scripts/publish.sh").read_text()
        return compile(script.split("<<'PY'\n")[-1].split("\nPY\n")[0], "publish-verification", "exec")

    def test_no_change_retry_verifies_all_source_files_not_only_homepage(self):
        source = self.directory / "source"
        target = self.directory / "clone/docs"
        source.mkdir()
        target.mkdir(parents=True)
        for name in ("index.html", "daily.html", "image.svg"):
            (source / name).write_text(name)
            (target / name).write_text(name)
        manifest = self.directory / "manifest.json"
        script = (ROOT / "scripts/publish.sh").read_text()
        code = script.split("<<'PY'\n")[-2].split("\nPY\n")[0]
        with patch.object(sys, "argv", ["-", str(target.parent), str(manifest), str(source)]), \
             patch("subprocess.check_output", return_value=b""):
            exec(compile(code, "publish-manifest", "exec"), {})
        self.assertEqual(set(json.loads(manifest.read_text())), {"index.html", "daily.html", "image.svg"})

    def test_live_verification_requires_matching_content(self):
        body = b"new page"
        manifest = self.directory / "manifest.json"
        manifest.write_text(json.dumps({"index.html": hashlib.sha256(body).hexdigest()}))
        response = MagicMock()
        response.__enter__.return_value = response
        response.status, response.read.return_value = 200, body
        argv = ["-", str(manifest), "https://example.invalid/", "1"]
        with patch.object(sys, "argv", argv), patch("urllib.request.urlopen", return_value=response), \
             patch("time.monotonic", return_value=0), contextlib.redirect_stdout(io.StringIO()) as output:
            exec(self.verification_code(), {})
        self.assertIn("线上校验通过 index.html", output.getvalue())
        response.read.return_value = b"stale page"
        with patch.object(sys, "argv", argv), patch("urllib.request.urlopen", return_value=response), \
             patch("time.monotonic", side_effect=[0, 0, 0, 0, 2, 2]), patch("time.sleep"), \
             contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(SystemExit, "线上校验超时"):
            exec(self.verification_code(), {})


if __name__ == "__main__":
    unittest.main()
