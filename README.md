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
 │     再用 arXiv 官方 API 按 id_list 核对元数据后入库。
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

数量不足、综合失败等情况会降级为 partial/empty 并以退出码 1 报告；已完成的解读有缓存，重跑不重复花费。

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
- `digest`：每期篇数下限/上限、补齐窗口 `lookback_windows_days`、单篇全文长度上限。
- `search`:DeepSeek Search 每次返回条数。
- `discovery`：检索 agent 的轮次/工具调用/候选数/单页字符预算。

## 命令

| 命令 | 说明 |
|---|---|
| `daily` | 采集 + 全文解读 + 日报，一条命令（退出码：0 完成；1 部分完成/为空/不足/预算耗尽） |
| `digest` | 只对已在队列里的候选做解读和日报，不主动检索 |
| `discover` | 只运行检索 agent 入库（`--since/--until` 指定窗口，默认近 7 天） |
| `pending` | 查看全文解读待处理队列 |
| `preview` | 用已有解读重新渲染日报与归档，不联网不调模型 |
| `papers` | 将本地中文 TeX 工程或已有中文 PDF 构建为全文专栏，不联网不调模型 |
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

浏览器看日报（本地静态服务）：

```bash
python -m http.server 8080 --bind 127.0.0.1 -d reports/site
# 然后打开 http://127.0.0.1:8080/
```

## 中文论文专栏

首批收录 **OneTrans、HSTU、OneReason**，目录记录在 `paper_catalog.json`。
三篇使用对应版本的 arXiv TeX 源码翻译并构建 HTML，保留正文、附录、公式、图表和参考文献。
`papers` 负责转换已翻译的本地工程，不会自动下载或翻译其他论文。

```bash
# 构建三篇全文、论文列表和网站首页
python pipeline.py papers

# 只重建指定论文（简称或 arXiv ID，--paper 可以重复）
python pipeline.py papers --paper OneTrans --paper HSTU

# 强制重新转换，不使用缓存
python pipeline.py papers --force

# 在浏览器中查看
python -m http.server 8080 --bind 127.0.0.1 -d reports/site
# http://127.0.0.1:8080/papers/
```

论文正文以可选择、搜索和重排的 HTML 展示，保留章节、参考文献与附录。
TeX 公式转换为浏览器原生 MathML，并保留对应的 TeX 标注；表格为可选择文字的 HTML 表格，
图形使用源文件中的图片或矢量图。公式、图表和逐条参考文献均保留交叉引用。
数学表达式、表格数据、图内文字、作者名与文献题名保留原文；标题、正文和图表说明译为中文。
“已有中文 PDF”入口提供此前的中文稿对照，当前网页译文由 TeX 独立构建。

`papers` 不调用模型、检索或日报数据库，不需要 API 密钥。转换缓存按整个 TeX 工程内容和转换器代码更新，
重复执行会复用正文与图表，同时刷新页面样式、列表和导航。
构建论文会保留已有日报；之后生成日报也会保留论文专栏。

构建 TeX 需要 LaTeXML。可以使用系统安装的 `latexmlc` / `latexmlpost`，或通过 `LATEXML_ROOT`
指定已解包且包含 `usr/bin/latexmlc` 的安装目录。目录中的 `latexml_root` 字段可为单篇指定安装位置。
当前三篇的原始源码、中文工程和转换工具保留在 `/tmp/recsys-tex-html-bdqa9f70/`，
分别位于 `<论文简称>/source/`、`<论文简称>/translated/` 和 `tools/latexml-root/`。
迁移机器或清理 `/tmp` 前，应保存需要继续编辑的中文工程，并更新 `paper_catalog.json` 中的路径；
已发布的 `docs/` 是独立静态网页，浏览时不依赖这些临时文件或 LaTeXML。

新增 TeX 条目需填写 `tex_project`（中文工程路径）与 `tex_entry`（如 `main.tex`），以及标题、简称、
arXiv ID/版本、公司和主题。配置了 TeX 但工程缺失时会明确报错。
没有 TeX 配置的旧条目仍可使用 `zh_pdf`，其路径相对 `--source-dir`（默认 `../LLM4Rec/source/`）；
缺少中文 PDF 时跳过。这种模式的公式与复杂图表采用 PDF 局部截图，新增论文需单独检查。

## GitHub Pages 在线阅读

启用后访问：https://decimal2.github.io/recsys_frontier_stats/。

仓库的 `docs/` 保存可公开阅读的日报和论文静态快照。在 GitHub 仓库的 **Settings → Pages** 中，
将 Source 设为 **Deploy from a branch**，选择 **main** 分支和 **/docs** 目录，点击 Save。
GitHub Pages 会发布首页、日报和中文论文。

本地生成新日报后，将页面同步到发布目录并提交到 GitHub：

```bash
mkdir -p docs
cp -R reports/site/. docs/
touch docs/.nojekyll
git add docs
git commit -m "Update published research pages"
git push origin main
```

GitHub Pages 展示已生成的静态页面；采集和模型调用仍由 `pipeline.py daily` 执行。
应递归复制整棵 `reports/site/`，以包含论文子目录、图表、中文 PDF 和目录 JSON。
不需要上传 `.env`、数据库或 `reports/paper_cache/` 转换缓存。

## 目录

```
data/papers.sqlite3            论文、运行记录、解读队列、日报 editions、任务记录(唯一数据库)
data/agent_checkpoints.sqlite3 LangGraph checkpoint(resume 用)
data/fulltext/<id>v<n>/        全文缓存(source + document.json)
data/insights/<sha>.json       单篇解读缓存(daily- 前缀为综合缓存)
reports/daily/<日期>.md/.json  日报
paper_catalog.json             中文论文目录（首批三篇）
reports/site/                  双专栏首页与各期日报
reports/site/daily/            日报归档
reports/site/papers/           中文论文列表、全文、图表与中文 PDF
reports/paper_cache/           中文正文转换缓存（不发布）
docs/                          GitHub Pages 发布快照
logs/<日期>/<时间>.log         每次运行的独立日志
```

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -q
```
