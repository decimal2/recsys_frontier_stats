# recsys_frontier_stats

推荐系统前沿论文的每日收集 agent：模型自主检索 arXiv 论文 → 全文阅读并生成带证据定位的中文解读 → 汇总发布每日日报（Markdown / JSON / HTML）。

整条流水线只有一个产品：**日报**。一条命令跑完：

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
       并更新归档首页 reports/site/index.html(浏览器直接打开即可)。
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

## 目录

```
data/papers.sqlite3            论文、运行记录、解读队列、日报 editions、任务记录(唯一数据库)
data/agent_checkpoints.sqlite3 LangGraph checkpoint(resume 用)
data/fulltext/<id>v<n>/        全文缓存(source + document.json)
data/insights/<sha>.json       单篇解读缓存(daily- 前缀为综合缓存)
reports/daily/<日期>.md/.json  日报
reports/site/                  日报网页与归档首页(浏览器打开 index.html)
logs/<日期>/<时间>.log         每次运行的独立日志
```

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -q
```
