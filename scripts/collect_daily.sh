#!/usr/bin/env bash
# Run from any directory; pipeline.py owns dates, configuration and logging.
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="$project_dir/.venv/bin/python"

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    cat <<'HELP'
用法：bash scripts/collect_daily.sh [daily 参数]

默认收集北京时间当天的日报，沿用 pipeline_config.json 的 provider 和篇数配置。
示例：
  bash scripts/collect_daily.sh
  bash scripts/collect_daily.sh --date 2026-10-27 --provider deepseek --limit 10

自动使用项目 .venv，无需 activate。日志：logs/YYYY-MM-DD/HH-MM-SS.log。
退出码沿用 pipeline.py：0 完成，1 失败、部分完成或篇数不足。
HELP
    exit 0
fi

if [[ ! -x "$python_bin" ]]; then
    printf '[ERROR] 找不到 %s，请先按 README 创建虚拟环境并安装依赖。\n' "$python_bin" >&2
    exit 1
fi
cd -- "$project_dir"
exec "$python_bin" -u pipeline.py daily "$@"
