#!/usr/bin/env bash
# Publish generated static files through a temporary clone, never the working Git tree.
set -Eeuo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="$project_dir/.venv/bin/python"
publish_repo="${PUBLISH_REPO_URL:-https://github.com/decimal2/recsys_frontier_stats.git}"
publish_branch="${PUBLISH_BRANCH:-main}"
site_url="${PUBLISH_SITE_URL:-https://decimal2.github.io/recsys_frontier_stats/}"
verify_timeout="${PUBLISH_VERIFY_TIMEOUT:-600}"
dry_run=false
verify=true
for argument in "$@"; do
    case "$argument" in
        --dry-run) dry_run=true ;;
        --no-verify) verify=false ;;
        --help|-h)
            cat <<'HELP'
用法：bash scripts/publish.sh [--dry-run] [--no-verify]

将 reports/site/ 中已生成的网页发布到 GitHub，不采集、不调用模型。
默认：克隆 main → 检查/同步 docs → 提交并推送 → 等待线上文件哈希一致。
--dry-run    只克隆、检查并显示差异，不提交、不推送。
--no-verify  推送后结束，不等待 GitHub Pages；不代表部署已成功。

使用本机 Git 登录与 user.name/user.email；不使用 Codex 的连接器凭据。
可覆盖：PUBLISH_REPO_URL、PUBLISH_BRANCH、PUBLISH_SITE_URL、PUBLISH_VERIFY_TIMEOUT（秒，默认 600）。
日志：logs/YYYY-MM-DD/HH-MM-SS.log。
HELP
            exit 0 ;;
        *) printf '[ERROR] 未知参数：%s\n' "$argument" >&2; exit 2 ;;
    esac
done

log_dir="$project_dir/logs/$(TZ=Asia/Shanghai date +%F)"
mkdir -p -- "$log_dir"
log_stem="$(TZ=Asia/Shanghai date +%H-%M-%S)"
log_path="$log_dir/$log_stem.log"
sequence=0
while ! (set -o noclobber; : > "$log_path") 2>/dev/null; do
    [[ -e "$log_path" ]] || { printf '[ERROR] 无法创建日志：%s\n' "$log_path" >&2; exit 1; }
    sequence=$((sequence + 1))
    log_path="$log_dir/$log_stem-$sequence.log"
done
exec > >(tee -a "$log_path") 2>&1
log() { printf '%s [%s] publish: %s\n' "$(TZ=Asia/Shanghai date +%FT%T%z)" "$1" "$2"; }
publish_tmp=""
failed_line=""
cleanup() {
    status=$?
    if (( status != 0 )); then
        log ERROR "发布失败 line=${failed_line:-unknown} exit_code=$status；请查看上方完整错误。"
    fi
    if [[ -n "$publish_tmp" ]]; then rm -rf -- "$publish_tmp"; fi
    log INFO "运行结束 exit_code=$status 日志=$log_path"
}
trap 'failed_line=$LINENO' ERR
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
log INFO "发布开始；只使用已有静态网页。日志=$log_path"
[[ "$verify_timeout" =~ ^[1-9][0-9]*$ ]] || { log ERROR 'PUBLISH_VERIFY_TIMEOUT 必须为正整数'; exit 2; }
[[ -x "$python_bin" ]] || { log ERROR '请先创建项目 .venv 并安装依赖'; exit 1; }
[[ -s "$project_dir/reports/site/index.html" ]] || { log ERROR '缺少 reports/site/index.html，请先运行采集脚本或 pipeline.py preview'; exit 1; }
command -v git >/dev/null
export GIT_TERMINAL_PROMPT=0
publish_tmp="$(mktemp -d "${TMPDIR:-/tmp}/recsys-publish.XXXXXX")"

# Credentials belong in Git's credential helper, not in repository URLs or logs.
"$python_bin" - "$publish_repo" <<'PY'
import sys
from urllib.parse import urlsplit
url = urlsplit(sys.argv[1])
if url.scheme in {"http", "https"} and (url.username or url.password):
    raise SystemExit("[ERROR] 仓库 URL 不得包含凭据；请使用 Git credential helper 或 SSH。")
PY
log INFO "克隆发布分支 $publish_branch"
git clone --depth 1 --single-branch --branch "$publish_branch" -- "$publish_repo" "$publish_tmp/repo"

"$python_bin" - "$project_dir/reports/site" "$publish_tmp/repo/docs" "$site_url" <<'PY'
import shutil
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

source, target = map(Path, sys.argv[1:3])
if target.is_symlink() or any(path.is_symlink() for path in target.rglob("*")):
    raise SystemExit("[ERROR] 远端 docs 含符号链接，无法安全同步静态站点。")
allowed = {".html", ".css", ".js", ".json", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".otf", ".txt", ".xml"}
files = []
for path in source.rglob("*"):
    relative = path.relative_to(source)
    if path.is_symlink():
        raise SystemExit(f"[ERROR] 发布源不允许符号链接：{relative}")
    if path.is_file():
        if relative.as_posix() != ".nojekyll" and (any(part.startswith(".") for part in relative.parts) or path.suffix.lower() not in allowed):
            raise SystemExit(f"[ERROR] 非静态发布文件：{relative}（PDF、日志、密钥和缓存不可发布）")
        files.append((path, relative))
for path, relative in files:
    destination = target / relative
    if any(p.is_symlink() for p in [destination, *destination.parents]):
        raise SystemExit(f"[ERROR] 发布目标不允许符号链接：{relative}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, destination)
(target / ".nojekyll").touch()

class Links(HTMLParser):
    def handle_starttag(self, tag, attrs):
        self.links.extend(value for key, value in attrs if key in {"src", "href"} and value)

prefix = urlsplit(sys.argv[3]).path.rstrip("/") + "/"
missing = []
for page in target.rglob("*.html"):
    parser = Links()
    parser.links = []
    parser.feed(page.read_text(encoding="utf-8"))
    for value in parser.links:
        url = urlsplit(value)
        if url.scheme or url.netloc or not url.path:
            continue
        path = unquote(url.path)
        if path.startswith("/"):
            path = path[len(prefix):] if path.startswith(prefix) else path.lstrip("/")
            link = target / path
        else:
            link = page.parent / path
        if not link.resolve().is_relative_to(target.resolve()) or not link.exists():
            missing.append(f"{page.relative_to(target)} -> {value}")
if missing:
    raise SystemExit("[ERROR] 本地链接或资源缺失：\n" + "\n".join(missing))
print(f"[INFO] 已同步 {len(files)} 个静态文件；本地链接检查通过，保留远端其余页面。")
PY

git -C "$publish_tmp/repo" add -- docs
git -C "$publish_tmp/repo" diff --cached --check
git -C "$publish_tmp/repo" diff --cached --stat
if "$dry_run"; then
    log INFO 'dry-run 检查完成，未提交或推送。'
    exit 0
fi

"$python_bin" - "$publish_tmp/repo" "$publish_tmp/manifest.json" "$project_dir/reports/site" <<'PY'
import hashlib
import json
import subprocess
import sys
from pathlib import Path
repo = Path(sys.argv[1])
names = subprocess.check_output(["git", "-C", str(repo), "diff", "--cached", "--name-only", "-z", "--", "docs"]).decode().split("\0")
names = [name for name in names if name and name != "docs/.nojekyll"]
if not names:
    # A previous push may have succeeded before deployment timed out. Recheck
    # every source file, not just an unchanged homepage that could hide stale pages.
    source = Path(sys.argv[3])
    names = ["docs/" + path.relative_to(source).as_posix() for path in source.rglob("*")
             if path.is_file() and path.name != ".nojekyll"]
manifest = {str(Path(name).relative_to("docs")): hashlib.sha256((repo / name).read_bytes()).hexdigest() for name in names}
Path(sys.argv[2]).write_text(json.dumps(manifest), encoding="utf-8")
PY

if git -C "$publish_tmp/repo" diff --cached --quiet; then
    log INFO '远端网页与本地一致，无需新提交。'
else
    for key in user.name user.email; do
        value="$(git -C "$project_dir" config --get "$key" || true)"
        if [[ -n "$value" ]]; then git -C "$publish_tmp/repo" config "$key" "$value"; fi
    done
    git -C "$publish_tmp/repo" commit -m "publishing: update generated site $(TZ=Asia/Shanghai date +%F)"
    git -C "$publish_tmp/repo" push origin "HEAD:refs/heads/$publish_branch"
    log INFO '网页已推送；GitHub Pages 将自动部署。'
fi
commit="$(git -C "$publish_tmp/repo" rev-parse HEAD)"
log INFO "发布提交 $commit"
if ! "$verify"; then
    log WARN '已跳过线上校验；推送成功不等于部署完成。'
    exit 0
fi

log INFO "等待线上文件更新，最长 $verify_timeout 秒。"
"$python_bin" - "$publish_tmp/manifest.json" "$site_url" "$verify_timeout" <<'PY'
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

pending = json.loads(Path(sys.argv[1]).read_text())
base = sys.argv[2].rstrip("/") + "/"
deadline = time.monotonic() + int(sys.argv[3])
while pending and time.monotonic() < deadline:
    for path, expected in list(pending.items()):
        if time.monotonic() >= deadline:
            break
        try:
            request = urllib.request.Request(base + quote(path), headers={"Cache-Control": "no-cache", "User-Agent": "recsys-pages-verification"})
            with urllib.request.urlopen(request, timeout=min(20, max(1, deadline - time.monotonic()))) as response:
                actual = hashlib.sha256(response.read()).hexdigest()
                status = response.status
            if status == 200 and actual == expected:
                print(f"[INFO] 线上校验通过 {path} HTTP=200 sha256={actual}", flush=True)
                del pending[path]
            else:
                print(f"[WARN] 页面尚未更新 {path} HTTP={status}", flush=True)
        except (OSError, urllib.error.URLError) as error:
            print(f"[WARN] 线上请求失败 {path}: {error}", flush=True)
    if pending:
        time.sleep(max(0, min(10, deadline - time.monotonic())))
if pending:
    raise SystemExit("[ERROR] 线上校验超时。提交已保留在远端，可检查 GitHub Actions 后重跑发布脚本。未更新：" + ", ".join(pending))
PY
log INFO "发布与线上校验完成：$site_url"
