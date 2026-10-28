"""Compare a local Pages snapshot with its public HTTP responses."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import logging
import os
from pathlib import Path
import time
from urllib.parse import quote
from urllib.request import Request, urlopen


LOG = logging.getLogger(__name__)


def default_site_url(repository):
    owner, name = repository.split("/", 1)
    suffix = "" if name.lower() == f"{owner}.github.io".lower() else name + "/"
    return f"https://{owner}.github.io/{suffix}"


def snapshot_hashes(site_dir):
    files = {}
    for path in sorted(site_dir.rglob("*")):
        relative = path.relative_to(site_dir)
        # Pages configuration files are not public page resources.
        if any(part.startswith(".") for part in relative.parts) or relative.as_posix() == "CNAME":
            continue
        if path.is_symlink():
            raise ValueError(f"Symlink is not a published snapshot file: {relative}")
        if path.is_file():
            files[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    if "index.html" not in files:
        raise ValueError("Snapshot must contain index.html")
    return files


def verify_snapshot(expected, site_url, timeout=600, interval=10, workers=4):
    deadline = time.monotonic() + timeout
    pending = dict(expected)
    verified = {}
    errors = {}

    def check(item):
        path, wanted = item
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return path, None, "Verification deadline reached"
        url = site_url.rstrip("/") + "/" + quote(path, safe="/")
        try:
            request = Request(url, headers={"User-Agent": "recsys-pages-verification", "Cache-Control": "no-cache"})
            with urlopen(request, timeout=min(10, remaining)) as response:
                body = response.read()
                status = response.status
            actual = hashlib.sha256(body).hexdigest()
            if status != 200 or actual != wanted:
                return path, None, f"HTTP {status}; expected SHA256 {wanted}, got {actual}"
            return path, actual, None
        except Exception as error:
            return path, None, f"{type(error).__name__}: {error}"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        while pending:
            # Wait for the deployed homepage before requesting the entire site.
            batch = [("index.html", pending["index.html"])] if "index.html" in pending else list(pending.items())
            for path, actual, error in pool.map(check, batch):
                if error is None:
                    verified[path] = actual
                    pending.pop(path)
                    errors.pop(path, None)
                else:
                    errors[path] = error
                    LOG.warning("%s: %s", path, error)
            LOG.info("Verified %d/%d files; pending %d", len(verified), len(expected), len(pending))
            remaining = deadline - time.monotonic()
            if not pending or remaining <= 0:
                break
            if len(batch) != 1 or batch[0][0] != "index.html" or "index.html" in pending:
                time.sleep(min(interval, remaining))
    return {
        "status": "passed" if not pending else "failed",
        "site_url": site_url,
        "commit": os.environ.get("GITHUB_SHA"),
        "total": len(expected),
        "verified": [{"path": path, "sha256": verified[path]} for path in sorted(verified)],
        "failed": [{"path": path, "expected_sha256": pending[path],
                    "error": errors.get(path, "Deployment did not become ready before deadline")}
                   for path in sorted(pending)],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-dir", type=Path, default=Path("docs"))
    parser.add_argument("--site-url", default=os.environ.get("PAGES_SITE_URL"))
    parser.add_argument("--report", type=Path, default=Path("artifacts/pages-verify/report.json"))
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--interval", type=float, default=10)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    if not args.site_url:
        repository = os.environ.get("GITHUB_REPOSITORY")
        if not repository:
            parser.error("Provide --site-url or set PAGES_SITE_URL / GITHUB_REPOSITORY")
        args.site_url = default_site_url(repository)
    if args.timeout <= 0 or args.interval <= 0 or args.workers <= 0:
        parser.error("timeout, interval and workers must be positive")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    try:
        report = verify_snapshot(snapshot_hashes(args.site_dir), args.site_url,
                                 args.timeout, args.interval, args.workers)
    except Exception as error:
        LOG.exception("Publication verification failed")
        report = {"status": "failed", "error": f"{type(error).__name__}: {error}"}
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log_result = LOG.info if report["status"] == "passed" else LOG.error
    log_result("Publication verification %s; report: %s", report["status"], args.report)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
