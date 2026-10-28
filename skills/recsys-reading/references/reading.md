# 全文与页面流程

所有代码从仓库根目录执行。先查看 `paper_pipeline/tex_html.py`、`paper_pages.py` 的当前签名；以下调用不启动 CLI、模型或检索。

## 工具与转换

页面包装需要 `beautifulsoup4`。TeX → HTML 需要 LaTeXML 的 `latexmlc` / `latexmlpost`，可通过 `latexml_root` 指定工具根目录。现成中文 HTML 可以跳过 LaTeXML；缺工具时安装所需转换工具或说明阻塞，不输出伪造全文。下载、解压时拒绝越界路径和危险符号链接，不执行不可信源码中的 shell 命令。

```python
from pathlib import Path
from paper_pipeline.tex_html import convert_tex

document = convert_tex(
    source_dir=Path(task_dir) / "source", entrypoint="main.tex",
    asset_dir=Path(task_dir) / "page/assets",
    build_dir=Path(task_dir) / "html-build",
)
```

主入口从实际源码确定，不能假定叫 main.tex。先检查 `digest.log` / `post.log`、错误节点和未解析引用，保留英文转换基线。转换兼容调整放临时副本，不能改原始论文含义。

`document` 包含 `body_html`、`toc`、`stats`、`extra_head`、`format`。在结构化 DOM 的自然语言节点上分批翻译，保护数学、SVG、代码、链接和数值；混排段落用可恢复占位符，回填后检查无遗漏、重复和占位符残留。章节正文与 `toc` 标题同步。统计反映真实文档。

TikZ 若失真，可在确认同一版本后，从对应 PDF 提取矢量区域替代图形；保留图号和中文说明，在 `stats.render_repairs` 记录来源、版本依据、SHA256、页码和裁切范围。页面只保留必要 SVG / PNG / WebP，不保留 PDF。

## 快照与目录

向 `paper_catalog.json` 的 `papers` 登记：`arxiv_id`（不带版本）、`version`、`short_name`、`title_en`、`title_zh`、`arxiv_url`、`companies`、`topics`、`collection`、`published_date`、`published_date_source` 与 `html_snapshot`。最后一个字段是相对目录清单的 `docs/papers/<id>/index.html`，日期来自官方首次提交记录；保留所有已有条目，检查唯一 ID。

```python
from paper_pipeline.paper_pages import render_paper

page_dir.mkdir(parents=True, exist_ok=True)
(page_dir / "index.html").write_text(render_paper(paper, document), encoding="utf-8")
```

`paper` 为目录条目，`page_dir` 对应论文页面目录，其下 `assets/` 保存平铺的必要图形。包装后必须有 `article.paper-fulltext` 和 `script#paper-document-metadata`；元数据包含正文重建所需 `format`、`toc`、`stats`、`extra_head`，不能只保存裸正文或 `/tmp` 资源路径。

## 不运行 pipeline 的离线建站

快照齐全后，复制当前完整 `docs/` 到临时目录的 `site/`，使用临时输出根目录：

```python
from paper_pipeline.paper_pages import build_papers
build_papers(output_dir=task_output, catalog_path=catalog_path, force=True)
```

`task_output` 包含 `site/`，`catalog_path` 是仓库目录清单绝对路径。此函数只处理已翻译的快照，不能从论文 ID 自动生成译文。

**先备份临时站点首页与日报归档。** 该函数还会调用 `publish_index(None, site)`，而新克隆没有被忽略的历史日报 JSON，可能生成空的日报首页。构建后恢复原 `daily/index.html`；将新首页的 `.library-preview` 和论文入口合并到原首页，保留原“最新日报”section。首次建站没有日报历史才可使用生成的空归档。不要把空历史覆盖用户已有日报。

在全新输出目录另做一次全部快照重建，检查没有缓存依赖。使用仓库浏览器审计：

```bash
CHROME_BIN="$(command -v google-chrome)" node tests/browser_audit.mjs \
  --site-dir /tmp/你的任务目录/site --output-dir /tmp/你的任务目录/audit
```

需要 Node.js（CI 使用 24）、Chrome 及合适的中英文字体。脚本按根目录 `paper_catalog.json` 检查全部论文；新增条目确保快照已经登记。查看 `report.json`、`geometry/`、`screenshots/`，并补充新增论文的重点截图。适当运行相关单元测试；未安装 LaTeXML 被跳过的测试要如实说明。

最后将已验证的完整站点按改动同步到 `docs/`，保留旧日报和其他论文；无意修改的文件不要提交。`papers/catalog.json` 是生成的公开目录，`paper_catalog.json` 是源清单，两者都需更新。
