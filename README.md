# recsys_frontier_stats

推荐系统与检索论文的每日阅读站：自动检索 arXiv、读取全文、生成带证据定位的中文解读并发布日报；同时提供中文论文全文阅读页。

在线：https://decimal2.github.io/recsys_frontier_stats/

## 快速开始

要求 Python 3.10+。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # 至少填入一个 provider 的 key
.venv/bin/python pipeline.py doctor
.venv/bin/python pipeline.py daily
```

运行后会生成 `reports/daily/<日期>.md|json` 和 `reports/site/<日期>.html`，并更新首页与归档。本地预览：

```bash
python -m http.server 8080 --bind 127.0.0.1 -d reports/site
# http://127.0.0.1:8080/
```

也可以由当前 harness 按 [skills/recsys-daily/SKILL.md](skills/recsys-daily/SKILL.md) 直接采集、解读并发布，无需单独配置模型 API。

## 工作方式

`daily` 一条命令完成：

1. **检索**：SearchAgent 调用 web_search / web_fetch 找候选，arXiv API 核对元数据。
2. **全文**：下载 arXiv HTML（失败回退 PDF），切成带 block id 的可引用文本。
3. **解读**：逐篇生成问题、方法、结果、局限和证据引用；JSON 结构校验，失败重试一次。
4. **补齐**：篇数不足时按 7 → 14 → 30 天扩窗。
5. **综合与发布**：生成趋势、专题和静态页面。

几条约定：

- 公司归属只来自作者署名机构，不从数据集或引用中的公司名猜测。
- insight 只做 JSON 结构校验，不做内容审计。
- `papers` 只从已翻译 HTML 快照重建，不调用模型，也不修改日报数据库。

## 配置

`.env` 至少配置一个 provider，默认 DeepSeek：

| 变量 | 用途 |
|---|---|
| `DEEPSEEK_API_KEY` | DeepSeek 模型 + 网页 Search/Fetch（网页工具固定走 DeepSeek） |
| `MOONSHOT_API_KEY` | Kimi，可选 |
| `ZAI_API_KEY` | GLM，可选 |

`pipeline_config.json` 主要配置：

- `companies` / `preferred_companies`：公司准入与优先团队。
- `digest`：篇数下限/上限（默认 5/10）、扩窗天数、全文长度上限。
- `llm`：默认 provider、超时、重试、`max_tokens`。
- `discovery`：检索轮次、工具调用、候选数和单页字符预算。

## 常用命令

| 命令 | 用途 |
|---|---|
| `.venv/bin/python pipeline.py daily` | 检索 + 全文解读 + 日报；退出码 0 完成，1 部分完成/为空/不足/预算耗尽 |
| `.venv/bin/python pipeline.py digest` | 只解读队列中的候选，不主动检索 |
| `.venv/bin/python pipeline.py discover` | 只运行检索 agent 入库（`--since/--until`，默认近 7 天） |
| `.venv/bin/python pipeline.py pending` | 查看待验证元数据 / 待全文解读的候选 |
| `.venv/bin/python pipeline.py preview` | 用已有解读离线重建日报，不联网 |
| `.venv/bin/python pipeline.py papers` | 从中文 HTML 快照重建论文页和目录 |
| `.venv/bin/python pipeline.py tasks` / `.venv/bin/python pipeline.py resume <id>` | 查看 / 恢复中断任务 |
| `.venv/bin/python pipeline.py doctor` | 离线检查配置、密钥与依赖 |

常用示例：

```bash
.venv/bin/python pipeline.py daily --date 2026-10-29 --limit 10
.venv/bin/python pipeline.py daily --provider kimi
.venv/bin/python pipeline.py papers --paper OneTrans --force
```

`fulltext` 和 `fetch` 是维护用子命令，参数见 `pipeline.py <命令> --help`。

## 每天采集与发布

```bash
bash scripts/collect_daily.sh                    # 北京时间当天
bash scripts/collect_daily.sh --date 2026-10-29  # 指定日期
bash scripts/publish.sh                          # 发布并等待线上校验
bash scripts/publish.sh --dry-run                # 只检查，不推送
```

`collect_daily.sh` 自动使用项目 `.venv`，参数原样传给 `daily`。`publish.sh` 会克隆远端，在临时目录中把 `reports/site/` 同步到 `docs/`，提交并核对线上文件哈希；首次使用需配置 Git 身份和 GitHub 写入权限（HTTPS 或 SSH），Pages 设置为 `main` 分支的 `/docs`。

发布后 `Verify published site` 和 `Paper rendering` 会自动运行，也可在 Actions 手动触发。运行日志位于 `logs/YYYY-MM-DD/`。失败候选可用 `pending` 查看，`resume` 恢复中断任务；更细的失败处理与发布约定见 [AGENTS.md](AGENTS.md)。

## 论文阅读

```bash
.venv/bin/python pipeline.py papers
.venv/bin/python pipeline.py papers --paper OneTrans --force
```

新增论文的固定版本源码、翻译、HTML 快照与验收流程见 [AGENTS.md](AGENTS.md)。

## 目录

```
pipeline.py                 CLI 入口
paper_pipeline/             检索、全文、解读、存储、页面构建
skills/recsys-daily/        harness 日报工作流
scripts/                    collect_daily.sh / publish.sh
docs/                       GitHub Pages 发布快照
reports/                    本地日报与 reports/site（不提交）
data/                       SQLite、缓存、任务记录（不提交）
paper_catalog.json          论文阅读目录
pipeline_config.json        日报配置
```

`docs/` 是发布源；`reports/`、`data/`、`logs/` 留在本地。

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -q
```

论文表格/公式的浏览器审计需要 Chrome：

```bash
CHROME_BIN="$(command -v google-chrome)" node tests/browser_audit.mjs \
  --site-dir docs --output-dir /tmp/paper-render-audit
```

## 更多

- 开发与维护约定：[AGENTS.md](AGENTS.md)
- Harness 日报技能：[skills/recsys-daily/SKILL.md](skills/recsys-daily/SKILL.md)
- 静态发布细节：[skills/references/publishing.md](skills/references/publishing.md)
