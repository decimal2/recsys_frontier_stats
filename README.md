# recsys_frontier_stats

推荐系统前沿论文的每日收集 agent：模型自主检索 arXiv 论文 → 全文阅读并生成带证据定位的中文解读 → 汇总发布每日日报（Markdown / JSON / HTML）。

网站包含两个专栏：**日报**追踪最新研究，**论文**提供已有中文译文的全文阅读页。
日报一条命令跑完：

```bash
python pipeline.py daily
```

## 工作流程

```
daily
 ├─ 1. 发现(discovery.py)
 │     LangGraph 检索 agent 调用 web_search / web_fetch 自主找论文,
 │     提交候选(bucket / priority / reason / source_url),
 │     再用 arXiv 官方 API 按 id_list 核对元数据后入库；失败候选保留待重试。
 ├─ 2. 全文(fulltext.py)
 │     下载 arXiv HTML(失败回退 PDF),切成带 block id 的可引用文本块。
 ├─ 3. 解读(insights.py)
 │     逐篇调用 LLM 生成结构化中文解读(问题/方法/结果/局限/证据引用),
 │     严格 JSON 结构校验,失败带纠错反馈重试一次。
 │     同时识别作者署名机构 → 公司白名单匹配并回写库。
 ├─ 4. 补齐(daily.py)
 │     不足 digest.min_papers 时,按 lookback_windows_days(7→14→30 天)
 │     逐级扩大窗口重新检索。
 ├─ 5. 综合(insights.py)
 │     对当天全部解读做一次 LLM 综合:趋势 + 今日专题。
 └─ 6. 发布(publishing.py)
       渲染 reports/daily/<日期>.md|.json 与 reports/site/<日期>.html,
       并更新双专栏首页 reports/site/index.html 和日报归档 daily/index.html。
```

数量不足、综合失败等情况会以 partial/empty/insufficient 和退出码 1 报告；已完成的解读有缓存，重跑复用。

## 安装与配置

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # 填入使用的 provider 密钥
```

依赖：Python 3.10+，beautifulsoup4、PyMuPDF、langgraph(+ sqlite checkpoint)。其余全部标准库。

`.env` 支持三个官方 provider（至少配一个，默认 deepseek）：

| 环境变量 | 提供商 | 用途 |
|---|---|---|
| `DEEPSEEK_API_KEY` | DeepSeek | 模型调用 + 网页 Search/Fetch 工具（网页工具固定走 DeepSeek） |
| `MOONSHOT_API_KEY` | Kimi | 可选替代模型 |
| `ZAI_API_KEY` | GLM | 可选替代模型 |

### pipeline_config.json

- `arxiv`:arXiv API 限速（请求间隔 ≥3 秒）、超时、重试、User-Agent。
- `companies` / `preferred_companies`：公司准入名单与优先团队（按作者署名机构判定，不从数据集/模型名猜测）。
- `llm`：默认 provider、超时、重试、`max_tokens`（所有模型调用共用的输出预算），以及各家模型与生成参数。
- `digest`：每期篇数下限/上限（默认 5/10 篇）、补齐窗口 `lookback_windows_days`、单篇全文长度上限。
- `search`:DeepSeek Search 每次返回条数。
- `discovery`：检索 agent 的轮次/工具调用/候选数/单页字符预算。

## 命令

| 命令 | 说明 |
|---|---|
| `daily` | 采集 + 全文解读 + 日报，一条命令（退出码：0 完成；1 部分完成/为空/不足/预算耗尽） |
| `digest` | 只对已在队列里的候选做解读和日报，不主动检索 |
| `discover` | 只运行检索 agent 入库（`--since/--until` 指定窗口，默认近 7 天） |
| `pending` | 分别查看待验证元数据的候选与待全文解读的论文 |
| `preview` | 用已有解读重新渲染日报与归档，不联网不调模型 |
| `papers` | 从已有中文 HTML 重建论文阅读页面和目录 |
| `fulltext --id <arxiv_id>` | 单独下载/导入一篇全文 |
| `fetch --url <url>` | 用 DeepSeek Fetch 保存网页 Markdown 证据 |
| `tasks` / `resume <task_id>` | 查看/恢复中断的 LangGraph 任务（checkpoint 在 data/ 下） |
| `doctor` | 检查配置、密钥与依赖，不联网 |

常用：

```bash
python pipeline.py daily                          # 今天
python pipeline.py daily --date 2026-10-23        # 指定归档日期
python pipeline.py daily --provider kimi          # 换模型
python pipeline.py daily --limit 12               # 本期上限(不得低于 digest.min_papers)
python pipeline.py preview                        # 改完样式后离线重建页面
```

### 日常使用的两个脚本

在项目目录运行，无需手动激活 `.venv`：

```bash
bash scripts/collect_daily.sh   # 收集北京时间当天日报，生成 HTML
bash scripts/publish.sh         # 发布 reports/site/，等待线上页面更新
```

采集脚本沿用 `pipeline_config.json`，当前默认 DeepSeek、下限 5 篇、上限 10 篇。参数直接传给 `daily`，例如补齐指定日期：

```bash
bash scripts/collect_daily.sh --date 2026-10-27 --provider deepseek --limit 10
```

后台采集当天日报，成功后自动发布，控制台输出追加到 `nohup.out`：

```bash
nohup bash -c 'bash scripts/collect_daily.sh && bash scripts/publish.sh' >> nohup.out 2>&1 &
```

`&&` 表示采集返回 0 才自动发布；篇数不足、部分完成或失败时停止后续发布，可查看日志处理后重跑。两种脚本都保留实际运行日期的 `logs/YYYY-MM-DD/HH-MM-SS.log`。

发布使用临时目录克隆远端 `main`，把已生成站点覆盖到克隆内的 `docs/`，只提交网页并保留远端其他文件；当前工作目录的 Git 历史和文件不参与提交。它不调用模型、不重新生成页面，也不改写项目本地 `docs/` 快照。需要重新渲染已有结果时，先运行 `.venv/bin/python pipeline.py preview`。

```bash
bash scripts/publish.sh --dry-run    # 克隆、检查链接和文件差异，不推送
bash scripts/publish.sh --no-verify  # 推送后结束，不等待线上校验
```

默认线上校验最长等待 600 秒，逐个比较本次改动文件的 HTTP 状态与 SHA256；超时会返回非零，但已经推送的提交仍保留。修复部署后再次执行即可，无变化时不会重复提交，会校验源站点的全部文件以确认上次部署结果。临时克隆在退出时自动清理。

**首次发布需配置本机 Git 的提交身份和 GitHub 写入权限。** Codex 连接器的登录不会自动提供给命令行：

```bash
git config --global user.name "你的 GitHub 用户名"
git config --global user.email "你的提交邮箱"
```

HTTPS 使用本机 Git credential helper；已安装 GitHub CLI 时可执行 `gh auth login`、`gh auth setup-git`。已配置 SSH 密钥时可运行：

```bash
PUBLISH_REPO_URL=git@github.com:decimal2/recsys_frontier_stats.git bash scripts/publish.sh
```

不要把密钥放进仓库 URL。可用环境变量覆盖 `PUBLISH_REPO_URL`、`PUBLISH_BRANCH`（默认 `main`）、`PUBLISH_SITE_URL` 和 `PUBLISH_VERIFY_TIMEOUT`（秒）。发布需要 GitHub Pages 已启用 `main` 的 `/docs`。

### 失败与补齐

单个网页的抓取超时、403/404 或临时服务错误会返回给检索模型，让它换源继续；认证、配置和程序错误仍会停止检索并记录完整堆栈。订阅预览、搜索错误页和空列表会被标记，失败 URL 在有限时间内不重复请求。

候选先持久化，再查询 arXiv 元数据。429 限流立即延后，其他临时网络错误最多即时重试一次；保留来源、选择理由、最后错误和下次重试时间，并遵守服务端的冷却时间。新的 `daily` 任务会先恢复历史失败候选并重试已到期的元数据，再按需检索；等待重试的 ID 不重复交给模型发现。成功验证后仍需通过日期和全文机构筛选。

`resume <task_id>` 恢复中断任务；对于已经结束但篇数不足的日报，重新运行 `daily` 补齐。已经结束的日报任务使用 `resume` 会返回原结果。已结束且仍有元数据失败的 `discover` 任务支持通过 `resume` 重试元数据，复用原检索结果。

例如补齐 10-27，日志由程序自动写入实际运行日期的目录：

```bash
source .venv/bin/activate
python pipeline.py pending
nohup python -u pipeline.py daily --provider deepseek --date 2026-10-27 --limit 10 > /dev/null 2>&1 &
```

查看 `logs/YYYY-MM-DD/HH-MM-SS.log`、`data/raw/run-*/agent-trace.json` 和日报 JSON 中的 `coverage`、`discovery`、`metadata_recovery`。候选提交数、元数据验证数和失败数分别统计；达到篇数下限仍取决于实际找到并验证的工业界论文，不通过放宽机构证据凑数。

浏览器看日报（本地静态服务）：

```bash
python -m http.server 8080 --bind 127.0.0.1 -d reports/site
# 然后打开 http://127.0.0.1:8080/
```

## 论文阅读专栏

论文阅读提供中文全文 HTML 浏览；新增和维护流程见 [AGENTS.md](AGENTS.md)。

```bash
# 从已发布 HTML 快照重建全文、论文列表和首页
python pipeline.py papers

# 只重建指定论文（简称或 arXiv ID，--paper 可以重复）
python pipeline.py papers --paper OneTrans --paper HSTU

# 忽略本地正文缓存，重新读取 HTML 快照
python pipeline.py papers --force

# 在浏览器中查看
python -m http.server 8080 --bind 127.0.0.1 -d reports/site
# http://127.0.0.1:8080/papers/
```

## GitHub Pages 在线阅读

启用后访问：https://decimal2.github.io/recsys_frontier_stats/

仓库的 `docs/` 保存可公开阅读的日报和论文静态快照。在 GitHub 仓库的 **Settings → Pages** 中，
将 Source 设为 **Deploy from a branch**，选择 **main** 分支和 **/docs** 目录，点击 Save。
GitHub Pages 会发布首页、日报和论文阅读专栏。

本地生成新日报后，用发布脚本同步并提交到 GitHub：

```bash
bash scripts/publish.sh
```

GitHub Pages 展示已生成的静态页面；采集和模型调用仍由 `pipeline.py daily` 执行。

`Verify published site` 会在 `main` 的 `docs/`、校验脚本或工作流更新后自动运行，也可在 Actions 手动触发。它按当前提交核对整个 `docs/` 的公开页面和资源的 HTTP 状态及 SHA256，最多等待 10 分钟让 Pages 更新；失败时返回非零，日志和 `report.json` 保存在 `pages-verify-*` Artifact。默认使用仓库的 GitHub Pages 地址，自定义域名可设置仓库 Actions 变量 `PAGES_SITE_URL`。`Paper rendering` 继续负责浏览器排版检查；`.vscode/` 仅保留在本地。

`scripts/publish.sh` 递归同步 `reports/site/`，包含论文子目录、图表和目录 JSON，自动补上 `.nojekyll`。
仅发布静态文件；PDF、隐藏文件（`.nojekyll` 除外）、符号链接和不支持的文件类型会中止发布。

## 目录

```
scripts/collect_daily.sh       采集当天或指定日期日报
scripts/publish.sh             独立克隆、同步静态站点、推送及线上校验
data/papers.sqlite3            论文、运行记录、元数据重试与解读队列、日报 editions、任务记录
data/agent_checkpoints.sqlite3 LangGraph checkpoint(resume 用)
data/raw/run-*/agent-trace.json 检索工具调用、结果和错误轨迹
data/raw/fetch-failures.json    有期限的失败 URL 缓存
data/fulltext/<id>v<n>/        全文缓存(source + document.json)
data/insights/<sha>.json       单篇解读缓存(daily- 前缀为综合缓存)
reports/daily/<日期>.md/.json  日报
paper_catalog.json             论文阅读目录
reports/site/                  双专栏首页与各期日报
reports/site/daily/            日报归档
reports/site/papers/           论文阅读列表、HTML 全文与图表
reports/paper_cache/           中文正文转换缓存（不发布）
docs/                          GitHub Pages 发布快照
logs/<日期>/<时间>.log         每次运行的独立日志
```

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -q
```

表格和公式还需要真实浏览器检查。安装 Chrome 后，可对发布目录运行：

```bash
CHROME_BIN="$(command -v google-chrome)" node tests/browser_audit.mjs \
  --site-dir docs --output-dir /tmp/paper-render-audit
```
