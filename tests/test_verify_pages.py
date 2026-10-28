import hashlib
import importlib.util
import json
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("verify_pages", Path(__file__).parents[1] / "scripts/verify_pages.py")
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


def digest(body):
    return hashlib.sha256(body).hexdigest()


class Response(BytesIO):
    status = 200


class VerifyPagesTests(unittest.TestCase):
    def test_snapshot_covers_nested_binary_and_excludes_pages_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "index.html").write_bytes(b"home")
            (root / ".nojekyll").touch()
            (root / "CNAME").write_text("example.com")
            (root / "papers").mkdir()
            (root / "papers/image.png").write_bytes(b"\x00\xff\x80")
            self.assertEqual(verify.snapshot_hashes(root), {
                "index.html": digest(b"home"), "papers/image.png": digest(b"\x00\xff\x80")})

    def test_stale_home_and_transient_asset_failure_are_retried(self):
        calls = []
        attempts = {}

        def fetch(request, timeout):
            url = request.full_url
            calls.append(url)
            attempts[url] = attempts.get(url, 0) + 1
            if url.endswith("index.html"):
                return Response(b"stale" if attempts[url] == 1 else b"home")
            if attempts[url] == 1:
                raise OSError("temporary connection failure")
            return Response(b"\x00\xff")

        with patch.object(verify, "urlopen", side_effect=fetch), patch.object(verify.time, "sleep"):
            report = verify.verify_snapshot({"index.html": digest(b"home"),
                                            "papers/a b.png": digest(b"\x00\xff")},
                                           "https://example.com/site/", timeout=10, workers=1)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(len(report["verified"]), 2)
        self.assertEqual(calls[:2], ["https://example.com/site/index.html"] * 2)
        self.assertEqual(attempts["https://example.com/site/papers/a%20b.png"], 2)

    def test_deadline_preserves_exact_failed_path_and_hash_error(self):
        with patch.object(verify, "urlopen", return_value=Response(b"wrong")), \
                patch.object(verify.time, "monotonic", side_effect=[0, 0, 2]):
            report = verify.verify_snapshot({"index.html": digest(b"home")},
                                           "https://example.com/", timeout=1, workers=1)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["failed"][0]["path"], "index.html")
        self.assertIn("HTTP 200; expected SHA256", report["failed"][0]["error"])

    def test_repository_pages_and_user_pages_urls(self):
        self.assertEqual(verify.default_site_url("owner/project"), "https://owner.github.io/project/")
        self.assertEqual(verify.default_site_url("owner/owner.github.io"), "https://owner.github.io/")

    def test_cli_failure_writes_readable_report_and_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            with patch("sys.argv", ["verify_pages.py", "--site-url", "https://example.com/",
                                    "--report", str(report)]), \
                    patch.object(verify, "snapshot_hashes", return_value={"index.html": "expected"}), \
                    patch.object(verify, "verify_snapshot", return_value={"status": "failed", "failed": ["index.html"]}), \
                    self.assertLogs(verify.LOG, level="ERROR"):
                self.assertEqual(verify.main(), 1)
            self.assertEqual(json.loads(report.read_text())["status"], "failed")


if __name__ == "__main__":
    unittest.main()
