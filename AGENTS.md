# Repository Guidelines

## 项目结构与工作边界

本项目包含「日报」与「论文阅读」两个专栏。以下命令均在 `recsys_frontier_stats/` 根目录执行。

- `pipeline.py`：CLI 入口；`paper_pipeline/`：检索、全文提取、解读、存储和页面构建。
- `scripts/collect_daily.sh`：采集当天/指定日期日报；`scripts/publish.sh`：从已有站点发布，独立克隆并校验线上内容。
- `prompts/`、`pipeline_config.json`：模型提示词与日报配置；`paper_catalog.json`：全文阅读目录。
- `paper_pipeline/paper_pages.py`：快照读取、页面包装、目录与离线重建；`tex_html.py`：LaTeXML 转换。
- `reading_tables.py`、`reading_math.py`：共享排版修复；修复应同时覆盖新转换、旧快照与缓存。
- `tests/`：单元测试与浏览器检查；`docs/`：GitHub Pages 发布快照及必要图片。
- `reports/site/`：本地生成网页；`data/`、`logs/`、`reports/paper_cache/`：运行数据，不提交。
- `paper_pdfs/`：本地中英文论文 PDF 归档（原名 `PDF_Papers/`），由 Git 忽略，不提交或发布。
- 用户原始论文资料不属于项目运行输入，清理时保留原件。

**`papers` 只从已翻译的 HTML 快照重建页面，不调用模型，也不修改日报数据库。** 新增论文从固定版本 arXiv 源码开始，在独立工作目录完成翻译与 HTML 转换，再登记发布快照。缺少中文内容且未安排翻译的论文不加入阅读目录，不将英文页面标为中文全文。

## 开发与运行命令

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python pipeline.py doctor
.venv/bin/python -m unittest discover -s tests -q
```

`doctor` 离线检查配置与依赖。只有需要模型调用时才配置 `.env`，使用 `.env.example` 中的变量。

- `.venv/bin/python pipeline.py daily`：联网检索、解读并生成日报，消耗 API 额度。
- `bash scripts/collect_daily.sh [daily 参数]`：自动使用项目 `.venv`，默认北京时间当天，保留 CLI 退出码。
- `bash scripts/publish.sh --dry-run`：检查发布差异；省略 `--dry-run` 会提交、推送并等待线上文件哈希一致，需要本机 Git 认证。
- `.venv/bin/python pipeline.py preview`：用已有解读离线重建日报。
- `.venv/bin/python pipeline.py papers`：离线重建全部论文及目录。
- `.venv/bin/python pipeline.py papers --paper OneTrans --force`：重建指定简称或 ID，重新读取 HTML 快照；`--paper` 可重复。
- `python3 -m http.server 8080 --bind 127.0.0.1 -d reports/site`：本地预览，论文入口为 `/papers/`。

## 新增论文与中文 HTML 流程

### 本地 PDF 资料归档

`paper_pdfs/source/en/` 保存英文 PDF，`paper_pdfs/source/zh/` 保存中文 PDF；沿用现有文件名及下载清单。这里的文件用于本地阅读、译文对照和图表核验，使用前确认论文 ID 与版本一致。它们是用户保留的资料，不是构建缓存，清理临时文件时不得删除。

`daily` 与 `papers` 不依赖此目录；新增论文仍按下述 arXiv 源码与 HTML 快照流程处理。不要将归档 PDF 复制到 `docs/`、`reports/site/` 或加入 `paper_catalog.json` 的运行输入，也不要强制提交被忽略的文件。

### 1. 确认来源与登记元数据

先检查目录是否已有同一 arXiv ID，避免重复。核对论文标题、作者署名机构、源码版本及全文范围，记录固定版本 `vN`；不要将不同版本的正文、图表与译文混用。

在 `paper_catalog.json` 的 `papers` 数组中登记：

| 字段 | 填写要求 |
|---|---|
| `arxiv_id`、`version`、`short_name` | ID 不带版本；版本单独填写，如 `v3`；简称供 `--paper` 选择 |
| `title_en`、`title_zh` | 原标题及准确译名 |
| `companies`、`topics`、`collection` | 公司来自作者署名；主题用于筛选；集合沿用现有分类 |
| `published_date`、`published_date_source` | 官方首次提交日期 `YYYY-MM-DD` 及来源 URL，不使用修订日期或从 ID 推算 |
| `html_snapshot` | 最终填写 `docs/papers/<arxiv_id>/index.html`，相对目录清单文件解析 |

可参考现有 OneTrans 条目。登记快照路径后，必须先生成对应文件再运行 `papers`。

### 2. 获取源码并转换为结构化 HTML

在 `/tmp` 创建独立任务目录，保存固定版本源码、构建结果和英文基线。检查 TeX 主入口、包含文件、图片、参考文献、附录是否齐全。新增全文优先使用 TeX；不要直接把 PDF 提取文本拼成全文。

使用现有转换器 `paper_pipeline.tex_html.convert_tex(source_dir, entrypoint, asset_dir, build_dir, latexml_root=None)`。它返回包含 `body_html`、`toc`、`stats`、`extra_head` 的文档，并生成图片资源和 `build_dir/result.json`。英文输入仍然输出英文，应保留此结果作为翻译前基线。

转换需要 `latexmlc`、`latexmlpost`；可配置 `LATEXML_ROOT` 指向解包后的工具根目录。检查 `digest.log`、`post.log` 与 LaTeXML 日志，修复错误节点、缺失图片和未解析引用；不得通过删除正文、公式或参考文献来消除错误。兼容宏包的调整放在临时副本或转换适配中，保留原始源码。

目录只接受项目内的相对 `html_snapshot` 路径。`papers --source-dir`、`zh_pdf`、`tex_project`、`tex_entry` 等旧本地导入入口已删除；不要恢复对某台机器论文目录的依赖。下载的 arXiv 源码使用上述 `convert_tex` 转换器单独处理，完成翻译与校验后再登记 HTML。

PPO 已有从英文 PDF 转换的例外；遇到其他只有 PDF 外壳的源码，先记录情况，不静默套用该例外。PDF 图形可转换为 SVG 或必要裁图，但发布目录不保留 PDF。

### 3. 翻译自然语言，保护论文结构

按章节和段落分批处理，为段落保留稳定标识，并建立术语表。翻译标题、摘要、正文、章节标题、图表说明、自然语言表头与附录；保留作者、机构、参考文献条目和原图中的英文。

仅修改自然语言节点，不让模型重写整页 HTML。保护 MathML、数学 TeX 注释、SVG、代码、实验数字、表格行列及合并关系、元素 ID、链接目标和图片路径。混合文本与公式的段落使用可还原占位符，回填后检查无缺失、重复或残留占位符。`toc` 标题应与中文正文同步。

逐段对照英文检查遗漏、误译、否定关系、条件与结论，保证正文、附录、参考文献齐全；全文翻译不能替换成 insight 摘要。表格文字允许翻译，数值、单位和对应关系必须保持。

### 4. 封装可重建的快照

使用 `paper_pipeline.paper_pages.render_paper(paper, document)` 生成站点页面；`paper` 可由 `load_catalog()` 读取，`document` 使用校对后的中文文档。先输出到任务暂存目录，连同 `assets/` 检查后再放入 `docs/papers/<arxiv_id>/`。

快照必须包含 `article.paper-fulltext` 和 `script#paper-document-metadata`。后者保存 `format`、`toc`、`stats`、`extra_head`；不要只保存裸正文或写死 `/tmp` 资源路径。资源使用同目录下的 `assets/<文件名>`；当前快照读取器复制该目录的直接子文件，应保持资源平铺。

更新统计使其反映实际文档。若因 TikZ 转换失真使用原论文图形替换，保留图号、说明、锚点，并在 `stats.render_repairs` 中记录来源、文件 SHA256、版本依据、页码和裁切范围；无法确认版本时明确标为未验证。

## 校验与验收

### 内容与结构检查

- 对照英文基线逐项检查公式内容、表格数值及行列、代码、图表、引用与章节顺序，不能只比较数量。
- 检查所有本地链接、目录锚点和图片存在；排查 `.ltx_ERROR`、`merror`、缺失资源与未解析引用。
- 翻译前后仅允许预期的自然语言变化；纯排版修改还应保持正文文本、MathML、SVG 和引用不变。
- 原图替换等必要差异单独说明并人工对照，不能靠修改统计数字掩盖缺失。

这些是全文页面验收要求。日报 insight 仍只做 JSON 结构校验，不把数字或引用内容审计重新变成日报生成的阻塞条件。

### 离线重建与测试

快照就绪后，使用全新输出目录验证不依赖历史缓存；全局 `--output-dir` 放在子命令之前：

```bash
paper_check_dir=$(mktemp -d /tmp/paper-rebuild-XXXXXX)
.venv/bin/python pipeline.py --output-dir "$paper_check_dir" papers --force
.venv/bin/python -m unittest discover -s tests -q
CHROME_BIN=google-chrome node tests/browser_audit.mjs \
  --site-dir "$paper_check_dir/site" --output-dir "$paper_check_dir/audit"
```

重建必须覆盖目录内全部论文，检查生成正文和资源完整、无临时文件依赖。仅 `--paper` 构建的全新目录不适合跑全站审计；浏览器脚本要求站点论文集合与根目录清单一致。LaTeXML 集成测试可能因缺依赖跳过，应如实报告。

### 真实浏览器检查

使用 Chrome 和 Node.js（CI 使用 Node 24），检查 1440px 与 390px。阅读 `report.json`、`geometry/` 和截图，并人工打开新增论文：

- 表格数字和模型名不逐字断行，表注不覆盖数据，合并单元格正确，宽表独立横向滚动。
- 短公式基线正常，长公式可滚动；页面整体不意外横向溢出。
- 图、SVG 标签、代码块和高亮文字清晰，无裁切、重叠或对比度问题。
- 章节、公式、文献跳转有效，日期排序与搜索、主题筛选组合正确。

审计会遍历全部论文，但重点截图目标由脚本中的 `cases` 指定；新增论文要补充必要截图或人工检查记录。共享排版修复需检查全部页面，不能只看最初报错的论文。浏览器无法运行时记录限制，不能把单元测试通过当作排版已通过。

## 发布、日志与清理

验收后构建正常输出，并同步完整站点：

```bash
.venv/bin/python pipeline.py papers --force
mkdir -p docs
cp -R reports/site/. docs/
touch docs/.nojekyll
git diff --check
git status --short
```

同步前确认已有日报与论文都在，保留首页、归档、论文目录、目录 JSON 和所有必要资源。阅读页日期排序依据首次提交日期。审阅差异并只提交本次文件，避免 `git add .` 混入用户的其他修改。

执行发布任务时提交并推送到目标仓库，确认 Pages 部署成功，再检查线上首页、列表、新论文和图片；必要时比对文件哈希。仅修改文档的任务不需要触发论文转换或模型调用。

日常发布可直接运行 `bash scripts/publish.sh`，只将 `reports/site/` 同步到临时克隆内的 `docs/` 并推送，不更改本地 Git 历史或 `docs/` 快照。`--no-verify` 只确认推送，不能据此宣称部署成功。脚本改动使用本地 bare Git 仓库和模拟 HTTP 响应测试，避免验证时意外发布或消耗模型额度。

CLI 自动将 INFO、WARNING、ERROR 和异常堆栈写入 `logs/YYYY-MM-DD/HH-MM-SS.log`，日期与时间是实际运行时间，不是日报归档日期。手工转换也应保留阶段、arXiv ID/版本、命令、退出码、完整异常与校验结果；不要只留下成功摘要或记录密钥。

上线验证后清理本次 `/tmp` 任务目录、下载包和不再需要的转换缓存，先保存必要诊断记录。保留 `docs/`、现有站点、日志及用户原始资料；不提交 API 密钥、数据库、缓存或 PDF 副本。发布快照必须能在缓存清理后独立重建。

## 编码、测试与提交约定

Python 3.10+，四空格缩进，函数和模块用 `snake_case`，类用 `PascalCase`；`.mjs` 沿用所在文件格式。未配置统一 formatter/linter，避免无关格式化。模型调用集中在 `providers.py`，数据库结构修改集中在 `storage.py`。

使用 `unittest`，文件命名 `tests/test_*.py`，方法命名 `test_*`；网络和模型调用用现有 fixture/FakeClient 模拟。没有数值覆盖率门槛，应覆盖改动行为及实际回归；文档修改无需新增测试。

检索容错改动需覆盖单页超时后换源、认证错误停止、订阅预览/错误页识别，以及元数据限流后的持久化与重试。候选应在元数据请求前落库，失败不丢弃来源证据；重试仍须通过日期、机构与历史去重检查。新的 `daily` 先恢复待验证候选，再按需搜索；已经结束的日报使用 `resume` 保持历史结果，补齐须新建 `daily` 任务。测试使用临时数据库与模拟响应，不改已有日报，不因离线测试通过就声称真实检索已补足篇数。

提交沿用简短范围前缀，如 `publishing: widen layout`、`discovery: tolerate metadata batch failures`。PR 说明问题、最终行为、验证结果和相关 issue；视觉修改附桌面与手机截图，明确未验证部分。
