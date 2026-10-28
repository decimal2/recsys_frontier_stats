# 日报数据与静态构建

以下路径相对仓库根目录。先阅读 `paper_pipeline/publishing.py` 的 `render_daily` / `edition_cards`，结构随仓库演进，以当前函数为准。

## 数据契约

将 harness 生成的完整 JSON 保存为 `reports/daily/YYYY-MM-DD.json`，顶层字段：

- `date`：有效 ISO 日期；`provider` / `model`：实际执行 harness 的模型信息，未知时写 `harness` / `unspecified`，不要伪称使用了某家 API。
- `status`：成功用 `complete`，有未完成工作用 `partial` 或 `insufficient`；`errors`：`[{"arxiv_id":"...","error":"..."}]`。
- `items`：下面的单篇对象列表；`synthesis`：`{"trends":[{"title":"...","analysis":"...","paper_ids":["..."]}],"topic":{"title":"...","analysis":"...","paper_ids":["..."]}}`。
- 可另加 `coverage`、检索窗口、来源和运行元数据。篇数不足写入 JSON / 日志，不添加页面横幅。

单篇对象的字段：

```json
{
  "paper": {
    "arxiv_id": "实际论文ID", "title": "原文标题",
    "url": "固定版本官方摘要URL", "published": "首次提交ISO时间",
    "updated": "所读版本修订ISO时间", "version": "v1"
  },
  "companies": ["配置中的公司标准名称"],
  "preferred_companies": ["命中的优先公司"],
  "company_basis": "作者署名及其可核实来源",
  "selection": {"tags": ["主题"]},
  "insight": {
    "analysis": {
      "title_zh": "中文标题", "one_liner": "一句话总结",
      "in_scope": true, "rating": 4,
      "relevance_reason": "主题相关性", "recommendation_reason": "推荐理由",
      "problem": "研究问题", "method": "具体方法与取舍",
      "method_evidence_ids": ["E1"],
      "key_results": [{"finding": "带设置和基线的实验发现", "evidence_id": "E1"}],
      "insights": [{"idea": "工程推断", "is_inference": true, "evidence_ids": ["E1"]}],
      "limitations": ["明确的局限"],
      "affiliations": [{"organization": "公司标准名称", "evidence_id": "E2"}],
      "evidence": [
        {"id": "E1", "block_id": "Section 4 / Table 2", "quote": "短原文证据"},
        {"id": "E2", "block_id": "作者署名", "quote": "短署名证据"}
      ]
    },
    "source_url": "实际全文URL",
    "extraction": {"note": "实际阅读格式与范围"}
  }
}
```

这是结构示例，不能把示例文本当成论文内容。若全文中的图表未读取，不能声称已分析。引用只保留必要短片段。

## 不运行 pipeline 的渲染

先把当前 `docs/` 复制到临时工作目录的 `site/`，不删除文件。使用仓库根目录作 Python 工作目录；Python 标准库即可导入日报渲染器。

```python
import json
from pathlib import Path
from paper_pipeline.publishing import render_daily, read_paper_catalog

payload = json.loads(payload_path.read_text(encoding="utf-8"))
links = {p["arxiv_id"]: p["url"] for p in read_paper_catalog(site)}
markdown, html = render_daily(payload, paper_links=links)
(site / (payload["date"] + ".html")).write_text(html, encoding="utf-8")
payload_path.with_suffix(".md").write_text(markdown, encoding="utf-8")
```

`payload_path` 是本期 JSON 的 Path，`site` 是临时完整站点 Path。JSON 需检查上述字段类型、合法日期、唯一论文 ID，以及 `rating` 为 1–5 的整数。可以参考 `insights.py` 的结构检查；不要因此引入模型调用或完整 pipeline 依赖。

用 `edition_cards([payload], "../")` 生成归档卡片，用 `edition_cards([payload])` 生成首页卡片。通过 DOM 编辑 `site/daily/index.html` 的 `.archive`：按 href 替换本期卡片或插入新卡片，按 ISO 日期倒序；保留其他卡片与搜索控件。首页只替换包含 `a[href="daily/index.html"]` 的 section 内最新日报卡片，并按所有归档日期选最新一期，补历史日报不能把首页回退。保留论文预览和双专栏导航。

若首次建立空站点，可用 `publish_index(None, site)` 根据工作目录 `daily/*.json` 建立索引。已有归档却缺少历史 JSON 时，不能这样重建；沿用上面的 DOM 更新方式。若最新一期不是本期，保留其已有首页卡片。

检查临时站点后，只同步本期页面、归档和首页到 `docs/`。不要把 JSON、日志或采集缓存塞进公开目录；需要本地预览可将完整站点同步到 `reports/site/`，再打开静态服务器。
