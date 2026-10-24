# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目定位

推荐系统研究阅读站，包括**中文日报**与**论文阅读**两个专栏。`daily` 完成"模型自主检索 arXiv 论文 → 全文解读 → 日报综合 → 发布 Markdown/JSON/HTML"；`papers` 从中文 TeX 工程或已有中文 PDF 离线构建论文页面。Python 3.10+，依赖见 requirements.txt；TeX 转换另需 LaTeXML。

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
.venv/bin/python pipeline.py papers           # 离线构建中文论文全文（缺中文则跳过）
.venv/bin/python pipeline.py tasks / resume <task_id>

# 本地浏览日报
python -m http.server 8080 --bind 127.0.0.1 -d reports/site
```

## 架构要点

**主流水线**(全部 LangGraph 图,经 `runtime.py` 的 `execute_graph` 执行):

- `daily.py` 图:initialize → load_queue → (read_paper 循环 | expand_window→discover 补齐) → synthesize → publish。数量不足 `digest.min_papers` 时按 `digest.lookback_windows_days`(7→14→30 天)逐级扩窗重新检索。
- `discovery.py` 图:search(SearchAgent 的 model→tool 循环,工具为 web_search/web_fetch/submit_papers) → metadata(arXiv API 按 id_list 核对,**单批失败只跳过该批**) → ingest(入库+provenance)。候选必须带 agent 亲眼见过的 source_url。
- 任务元数据在**主库** `data/papers.sqlite3` 的 `agent_tasks` 表(DDL 集中在 `storage.py`,含从旧 `agent_checkpoints.sqlite3` 的幂等迁移);LangGraph checkpoint 仍在 `agent_checkpoints.sqlite3`,`resume` 靠它恢复现场。

**关键约定**:

- 所有与"日"相关的边界(日报日期、窗口)按 **UTC+8**(`arxiv.DAY_ZONE`);带时间戳的 ISO 输入仍按 UTC。
- LLM 调用只走 `providers.py` 的 `OfficialClient`(deepseek/kimi/glm,OpenAI 兼容);网页 Search/Fetch 固定走 DeepSeek。`IncompleteRead` 等截断错误和 429(30s 起步退避)都在重试集合内。
- `insights.py` 只做 JSON **结构**校验(失败带纠错反馈重试一次),不做内容审计——insight 是总结不是校验,不要往回加内容检查。
- insight/synthesis 按 prompt+输入+provider 的 sha256 缓存到 `data/insights/`,重跑不重复调用 LLM。
- 公司归属只能来自作者署名机构(`selection.py` 只做公司匹配,关键词打分体系已删除,不要恢复);规则文本只写在 `prompts/company_rule.txt`,其他 prompt 用 `{company_rule}` 占位符注入。
- `publishing.py` 渲染日报、公共导航和双专栏首页；`paper_pages.py` 管理论文目录、页面与离线构建；`tex_html.py` 使用 LaTeXML 将翻译的源码转换为原生 MathML、HTML 表格和逐条文献引用；`paper_html.py` 保留旧中文 PDF 转换能力。日报不得覆盖 `site/papers/`。
- `paper_catalog.json` 登记论文、来源与固定 arXiv 版本。已发布论文用 `html_snapshot`（相对目录文件）重建，正文与元数据都在 `docs/papers/<id>/index.html` 内；清理临时源码和缓存不影响重建。新增全文先在 `/tmp` 下载 TeX，经 LaTeXML 得到语义 HTML 后只翻译自然语言节点，公式、表格数据、图片和引文受保护；PPO 无正文 TeX，用户已允许仅该篇从英文 PDF 转换。发布且验证后清理本次缓存。`papers` 不运行检索、不调用 LLM、不修改日报数据库，仍兼容 `tex_project` + `tex_entry` 与旧 `zh_pdf` 输入。配置的输入缺失应明确报错，不能静默回退；必须保留全文与附录。
- CSS 内联，单篇 HTML 和图形资源位于 `site/papers/<id>/`。论文专栏不复制或保留下载用 PDF，构建时删除旧 `paper.zh.pdf` / `paper_zh.pdf` 副本。发布时递归复制 `reports/site/.` 到 `docs/`，不要只复制根目录 HTML；正文转换缓存 `reports/paper_cache/` 不发布。run 级报告仍已删除。
- CLI 进库前抢 fcntl 文件锁(`cli.py database_lock`),进锁后把残留 `status='running'` 的 run 标记为 failed。
- `daily`/`digest` 的 partial/empty/insufficient/budget_exhausted 状态映射为退出码 1。

**测试约定**:tests/ 用 unittest + 大量 mock(FakeClient/FakeFullText),不触网;`test_pipeline.py` 提供 `feed()`/`PAYLOAD` 等公共 fixture 供其他测试 import。
