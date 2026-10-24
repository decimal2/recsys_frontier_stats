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

## 论文阅读专栏

当前共 **38 篇**：`LLM4Rec/source` 的 31 篇与 `RL_Training/source` 的 7 篇，清单记录在 `paper_catalog.json`。
已发布的中文全文保存在 `docs/papers/<arxiv_id>/`，只包含 HTML 和必要图表，不保留下载用 PDF。
论文列表默认按 arXiv **首次提交日期**从新到旧排列，也可切换为最早在前，并与搜索、主题筛选组合使用。
日期来自官方元数据的 `published` 字段，记录为 `published_date` 与 `published_date_source`；修订日期不参与排序。

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

新增全文采用固定版本 arXiv TeX 源码，在 `/tmp` 独立任务目录中下载、解压并转换为结构化 HTML，
再翻译标题、正文、章节标题和图表说明。数学、表格数据、代码、原图、作者与参考文献保持原样。
逐段对照英文审阅，并核对公式、引用、图片和 HTML 结构后，发布到 GitHub Pages；上线验证后删除本次临时源码与构建缓存。
PPO 的 arXiv 源码只有 PDF 外壳，没有正文 TeX，因此单独从英文 PDF 转换和翻译，公式与图表保留原版裁图。

TeX 公式使用浏览器原生 MathML，表格为 HTML 表格，图形使用源码中的图片或矢量图；体积过大的密集矢量图以 216 dpi 无损 PNG/WebP 发布；WebP 编码前后逐像素核对。
全文保留正文、附录和逐条参考文献；章节、公式、图表与文献的交叉引用可直接跳转。
原图中的英文文字及参考文献题名保留原文。

`papers` 是离线重建命令，不调用模型、检索或日报数据库，不需要 API 密钥。
`html_snapshot` 路径相对目录清单文件，快照内保存正文和重建所需的元数据；
清理 `/tmp` 与 `reports/paper_cache/` 后仍可重建，无需重新下载源码或重新翻译。
构建论文会保留已有日报；之后生成日报也会保留论文专栏。

网页排版修复会在每次渲染时应用，也覆盖已有 HTML 快照和正文缓存：解除 TeX 固定尺寸/缩放盒，
恢复标准表格行列，让宽表格独立横向滚动，表注保持在表格外的正常文档流中。
表格中的下划线保持普通文字样式；高亮单元格和代码块保留原背景色，并修正不足的文字对比度。
正文取消两端强制对齐；浏览器只对实际超出正文宽度的行内公式添加横向滚动，普通行内公式保留原生基线。
SVG 图示采用适合原图的白底黑色默认文字；不能直接缩放的宽图整体横向滚动，避免裁切图中的公式和标签。
这些调整会核对正文、表格数值、合并单元格、MathML、链接和图片，避免用删减内容解决排版问题。

手动转换中文 TeX 工程仍可使用 `tex_project` + `tex_entry` 配置，需要系统的 `latexmlc` / `latexmlpost`，
或通过 `LATEXML_ROOT` / 单篇 `latexml_root` 指定解包工具目录。配置的 TeX 缺失时会明确报错。
旧条目的 `zh_pdf` 模式也保留兼容能力，路径相对 `--source-dir`（默认 `../LLM4Rec/source/`）。
这些输入模式同样只输出 HTML 和图形，并清除页面目录中旧 `paper.zh.pdf` / `paper_zh.pdf` 副本。

## GitHub Pages 在线阅读

启用后访问：https://decimal2.github.io/recsys_frontier_stats/

仓库的 `docs/` 保存可公开阅读的日报和论文静态快照。在 GitHub 仓库的 **Settings → Pages** 中，
将 Source 设为 **Deploy from a branch**，选择 **main** 分支和 **/docs** 目录，点击 Save。
GitHub Pages 会发布首页、日报和论文阅读专栏。

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
应递归复制整棵 `reports/site/`，以包含论文子目录、图表和目录 JSON。
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

GitHub Actions 的 **Paper rendering** 会在相关页面、渲染代码或测试改动时自动运行。
它按目录清单检查所有论文的 1440px / 390px 布局，检查表注与数据重叠、数字断行、意外溢出、
超宽公式滚动、短公式基线，以及日期排序与筛选组合；失败时退出非零。
在该次运行的 Artifacts 中下载 `paper-render-*`，查看 `report.json`、`geometry/` 和 `screenshots/`。
单元测试、链接检查或 MathML 数量一致，均不能代替这些渲染检查。
