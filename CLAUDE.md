# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目定位

推荐系统研究阅读站，包括**中文日报**与**论文阅读**两个专栏。`daily` 完成"模型自主检索 arXiv 论文 → 全文解读 → 日报综合 → 发布 Markdown/JSON/HTML"；`papers` 从已翻译的 HTML 快照重建论文页面。Python 3.10+，依赖见 requirements.txt；arXiv 源码转换另需 LaTeXML。

## 常用命令

```bash
# 全部测试(pytest 未安装,用 unittest)
.venv/bin/python -m unittest discover -s tests -q
# 单个测试
.venv/bin/python -m unittest tests.test_discovery_graph -q
.venv/bin/python -m unittest tests.test_pipeline.PipelineTests.test_xxx -q

# 运行(入口是根目录 pipeline.py;包内无 __main__.py,python -m paper_pipeline.cli 不会执行)
.venv/bin/python pipeline.py daily            # 主流程
.venv/bin/python pipeline.py doctor           # 离线检查配置/密钥/依赖
.venv/bin/python pipeline.py preview          # 离线重建日报 HTML
.venv/bin/python pipeline.py papers           # 从 HTML 快照重建论文阅读专栏
.venv/bin/python pipeline.py tasks / resume <task_id>

# 本地浏览日报
python -m http.server 8080 --bind 127.0.0.1 -d reports/site
```

## 架构要点

**主流水线**(全部 LangGraph 图,经 `runtime.py` 的 `execute_graph` 执行):

- `daily.py` 图:initialize → recover_metadata → load_queue → read_paper / 按需 discover → synthesize → publish。新的日报任务先恢复待验证候选；数量不足时按 7→14→30 天扩窗，优先处理窗口内已有队列，再调用模型检索。已结束日报的 `resume` 返回原结果，补齐使用新 `daily` 任务。
- `discovery.py` 图:search(SearchAgent 的 model→tool 循环,工具为 web_search/web_fetch/submit_papers) → metadata → ingest。候选在请求 arXiv 元数据前写入 `pending_metadata`；失败保留来源并延后重试，提交数与验证数分别统计。候选必须带 agent 亲眼见过的 source_url。
- 任务元数据在**主库** `data/papers.sqlite3` 的 `agent_tasks` 表(DDL 集中在 `storage.py`,含从旧 `agent_checkpoints.sqlite3` 的幂等迁移);LangGraph checkpoint 仍在 `agent_checkpoints.sqlite3`,`resume` 靠它恢复现场。

**关键约定**:

- 所有与"日"相关的边界(日报日期、窗口)按 **UTC+8**(`arxiv.DAY_ZONE`);带时间戳的 ISO 输入仍按 UTC。
- LLM 调用只走 `providers.py` 的 `OfficialClient`(deepseek/kimi/glm,OpenAI 兼容)；网页 Search/Fetch 固定走 DeepSeek。单页临时错误返回模型换源，认证和权限错误仍停止。arXiv 候选元数据遇到 429 立即延后并遵守端点冷却，其他临时错误最多即时重试一次。
- `insights.py` 只做 JSON **结构**校验(失败带纠错反馈重试一次),不做内容审计——insight 是总结不是校验,不要往回加内容检查。
- insight/synthesis 按 prompt+输入+provider 的 sha256 缓存到 `data/insights/`,重跑不重复调用 LLM。
- 公司归属只能来自作者署名机构(`selection.py` 只做公司匹配,关键词打分体系已删除,不要恢复);规则文本只写在 `prompts/company_rule.txt`,其他 prompt 用 `{company_rule}` 占位符注入。
- `publishing.py` 渲染日报、公共导航和双专栏首页；`paper_pages.py` 管理论文目录与 HTML 重建；`tex_html.py` 用于将下载的 arXiv 源码转换为原生 MathML、HTML 表格和逐条文献引用。旧中文 PDF 转换器已删除。日报不得覆盖 `site/papers/`。
- `paper_catalog.json` 只登记项目内的相对 `html_snapshot` 路径与固定 arXiv 版本。正文和元数据在 `docs/papers/<id>/index.html` 内，清理临时源码与缓存不影响重建。新增全文从固定版本 arXiv 源码开始，转换后翻译自然语言节点，保护公式、表格数据、图片和引文，再登记 HTML。`papers` 不运行检索、不调用 LLM、不修改日报数据库；不再接受 `--source-dir`、`tex_project`、`tex_entry`、`zh_pdf` 等本地导入入口。必须保留全文与附录，快照缺失应明确报错。
- CSS 内联，单篇 HTML 和图形资源位于 `site/papers/<id>/`。论文专栏不复制或保留下载用 PDF，构建时删除旧 `paper.zh.pdf` / `paper_zh.pdf` 副本。发布时递归复制 `reports/site/.` 到 `docs/`，不要只复制根目录 HTML；正文转换缓存 `reports/paper_cache/` 不发布。run 级报告仍已删除。
- CLI 进库前抢 fcntl 文件锁(`cli.py database_lock`),进锁后把残留 `status='running'` 的 run 标记为 failed。
- `daily`/`digest` 的 partial/empty/insufficient/budget_exhausted 状态映射为退出码 1。

**测试约定**:tests/ 用 unittest + 大量 mock(FakeClient/FakeFullText),不触网;`test_pipeline.py` 提供 `feed()`/`PAYLOAD` 等公共 fixture 供其他测试 import。
