"""Local daily editions, archive, and shared navigation for Chinese papers."""
import html
import json
import logging
import re
from pathlib import Path

from .selection import organization_details

# Single-file reading typography: neutral grays, one accent, dark mode via CSS variables.
STYLES = """
:root{
  --bg:#f7f7f5;--surface:#ffffff;--ink:#1f2421;--muted:#6b726e;
  --line:#e3e6e2;--accent:#0f766e;--accent-soft:#e5f3f0;--chip:#eef1ee;
  --notice-bg:#fdf6e3;--notice-line:#e7d9b8;--topic-bg:#173a35;--topic-ink:#eaf3f0;--topic-link:#7fd4c8;
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#141615;--surface:#1c1f1d;--ink:#e8eae7;--muted:#9aa39d;
    --line:#2e3330;--accent:#5ec4b8;--accent-soft:#16302d;--chip:#262a28;
    --notice-bg:#2e2a1c;--notice-line:#4a4130;--topic-bg:#122b27;--topic-ink:#e2efeb;--topic-link:#7fd4c8;
  }
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);-webkit-font-smoothing:antialiased;
  font:16px/1.75 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,
  "PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
header{max-width:1000px;margin:0 auto;padding:28px 24px 18px;display:flex;justify-content:space-between;
  align-items:baseline;gap:16px}
.brand{color:var(--ink);font-weight:700;font-size:14px;letter-spacing:.12em}
header span{font-size:12.5px;color:var(--muted)}
.site-nav{margin:0;padding:0;border:0;gap:8px 22px;font-size:14px}
.site-nav a{padding:4px 0;color:var(--muted)}
.site-nav a[aria-current="page"]{color:var(--accent);font-weight:600;border-bottom:2px solid var(--accent)}
main{max-width:1000px;margin:0 auto;padding:8px 24px 40px}
.eyebrow{font-size:12px;font-weight:600;letter-spacing:.18em;text-transform:uppercase;color:var(--accent)}
h1{font-size:30px;line-height:1.3;letter-spacing:-.01em;margin:6px 0 14px}
h2{display:flex;align-items:baseline;gap:14px;font-size:20px;letter-spacing:-.01em;margin:0 0 18px}
h2 .no{color:var(--accent);font-size:12.5px;font-weight:700;letter-spacing:.22em}
h3{font-size:17.5px;line-height:1.5;margin:0 0 8px}
h4{font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin:22px 0 8px}
p{margin:8px 0}
.lede{color:var(--muted);font-size:15.5px}
.muted{color:var(--muted);font-size:13px}
.metrics{display:flex;flex-wrap:wrap;gap:40px;margin:22px 0;padding:16px 0;
  border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.metrics div{font-size:12.5px;color:var(--muted)}
.metrics strong{display:block;font-size:26px;line-height:1.2;color:var(--ink)}
.notice{margin:16px 0;padding:12px 16px;font-size:14px;background:var(--notice-bg);
  border:1px solid var(--notice-line);border-radius:10px}
nav{display:flex;flex-wrap:wrap;gap:8px 20px;margin:26px 0 0;padding:12px 0;
  border-bottom:1px solid var(--line);font-size:13.5px}
section{margin-top:44px;scroll-margin-top:16px}
.trends{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px}
.trend{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:18px 20px}
.trend p{font-size:14.5px}
.sources{margin-top:10px;font-size:12.5px;color:var(--muted)}
.briefs{list-style:none;margin:0;padding:0;background:var(--surface);
  border:1px solid var(--line);border-radius:12px}
.briefs li{padding:14px 20px;border-top:1px solid var(--line)}
.briefs li:first-child{border-top:none}
.briefs li a{font-weight:600;color:var(--ink)}
.briefs li a:hover{color:var(--accent);text-decoration:none}
.briefs li span{display:block;margin-top:2px;font-size:13.5px;color:var(--muted)}
input{width:100%;margin:0 0 18px;padding:12px 16px;font:inherit;font-size:14.5px;color:var(--ink);
  background:var(--surface);border:1px solid var(--line);border-radius:10px}
input:focus{outline:2px solid var(--accent);outline-offset:1px;border-color:transparent}
.paper{background:var(--surface);border:1px solid var(--line);border-radius:12px;
  box-shadow:0 1px 2px rgb(0 0 0/.03);padding:26px 30px;margin:20px 0}
.paper-meta{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-bottom:10px;
  font-size:12.5px;color:var(--muted)}
.paper-meta>span{color:var(--accent);letter-spacing:.08em}
.pill{display:inline-block;padding:2px 10px;border-radius:999px;background:var(--accent-soft);
  color:var(--accent);font-size:12px;font-weight:600}
.original{display:inline-block;margin:2px 0 4px;font-size:13px;color:var(--muted);overflow-wrap:anywhere}
.reason{margin:14px 0;padding:2px 0 2px 14px;border-left:3px solid var(--accent);font-size:14.5px}
.paper ul{margin:6px 0;padding-left:22px}
li{margin:5px 0}
.insight{margin:18px 0;padding:14px 18px;background:var(--chip);border-radius:10px}
.insight h4{margin-top:4px;color:var(--accent)}
.insight small{margin-left:6px;font-weight:400;letter-spacing:0;text-transform:none;color:var(--muted)}
.topic{padding:24px 26px;border-radius:12px;background:var(--topic-bg);color:var(--topic-ink)}
.topic a{color:var(--topic-link)}
.archive{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,300px),1fr));gap:14px;margin-top:20px}
.edition{display:block;padding:20px 22px;background:var(--surface);border:1px solid var(--line);
  border-radius:12px;color:var(--ink)}
.edition:hover{border-color:var(--accent);text-decoration:none}
.edition h2{margin:4px 0 6px;font-size:18px}
.edition p{margin:0 0 8px;font-size:14px;color:var(--muted)}
.section-heading{display:flex;align-items:baseline;justify-content:space-between;gap:16px;margin-bottom:18px}
.section-heading h2{margin:0}
.section-heading>a{font-size:13.5px;white-space:nowrap}
.library-preview{display:grid;gap:12px}
.library-preview .edition h3{margin:4px 0 6px}
.library-preview .edition .original{display:block;font-size:12.5px}
.reading-link{display:inline-block;font-size:13px;margin:4px 0 8px;font-weight:600}
.empty{margin-top:20px;padding:36px 30px;background:var(--surface);
  border:1px dashed var(--line);border-radius:12px}
.steps{padding:14px 0;color:var(--accent);font-size:14px;font-weight:600}
[hidden]{display:none!important}
@media (max-width:640px){
  header{padding:22px 18px 14px}
  main{padding:4px 18px 32px}
  h1{font-size:25px}
  .metrics{gap:24px}
  .paper{padding:20px 18px}
  .topic{padding:20px}
}
"""


def md(text):
    text = html.escape(" ".join(str(text).split()), quote=False)
    for char in "\\`*_{}[]|":
        text = text.replace(char, "\\" + char)
    return text


def atomic_write(path, content):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def escape(value):
    return html.escape(str(value), quote=True)


def anchor(identifier):
    return "paper-" + re.sub(r"[^a-zA-Z0-9.-]", "-", identifier)


def page(title, body, subtitle="推荐系统 · 相关检索 · LLM", *, root_prefix="",
         section="daily", extra_head="", body_class=""):
    navigation = "".join(
        f'<a href="{escape(root_prefix + path)}"'
        + (' aria-current="page"' if section == name else '')
        + f'>{label}</a>'
        for name, path, label in (("daily", "daily/index.html", "日报"),
                                  ("papers", "papers/index.html", "论文阅读"))
    )
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title)}</title>
<style>{STYLES}</style>{extra_head}</head><body class="{escape(body_class)}">
<header><a class="brand" href="{escape(root_prefix)}index.html">REC / PAPER LAB</a><nav class="site-nav" aria-label="主导航">{navigation}</nav></header>
<main><div class="eyebrow">{escape(subtitle)}</div><h1>{escape(title)}</h1>{body}</main>
<script>const search=document.querySelector('#search'); if(search) search.addEventListener('input',()=>{{
 const q=search.value.toLowerCase();document.querySelectorAll('[data-search]').forEach(el=>{{el.hidden=!el.dataset.search.toLowerCase().includes(q)}});
}});</script></body></html>'''


def source_links(ids, by_id):
    return " · ".join(f'<a href="#{anchor(i)}">{escape(by_id[i]["insight"]["analysis"]["title_zh"])}</a>'
                      if i in by_id else escape(i) for i in ids)


def render_daily(payload, paper_links=None):
    items = payload["items"]
    by_id = {i["paper"]["arxiv_id"]: i for i in items}
    day = payload["date"]
    lines = [f"# 推荐研究日报 · {day}", "", payload["note"], "",
             f"模型：{payload['provider']} / {payload['model']}；状态：{payload['status']}。", "", "## 当日样本观察", ""]
    intro = f'<p class="lede">{escape(payload["note"])}</p><div class="metrics"><div><strong>{len(items)}</strong>全文解读</div><div><strong>{sum(bool(i["preferred_companies"]) for i in items)}</strong>优先团队</div><div><strong>{len(payload["errors"])}</strong>待重试</div></div>'
    coverage = payload.get("coverage", {})
    if coverage.get("shortfall"):
        message = (f"本期已完成 {len(items)} 篇工业界论文，最低要求 {coverage['minimum']} 篇，"
                   f"还差 {coverage['shortfall']} 篇。本期尚未完成，可重新运行 daily 继续补齐。")
        lines += [message, ""]
        intro += '<aside class="notice">' + escape(message) + '</aside>'
    if not items:
        message = ("本次全文解读未完成，尚未生成论文总结。请查看运行日志后重试。" if payload["errors"]
                   else "本次没有可生成全文解读的论文。可以检查日期范围，或稍后重新检索。")
        lines += [message, ""]
        if payload["errors"]:
            lines += ["## 待重试", ""] + [f"- {e['arxiv_id']}：{md(e['error'])}" for e in payload["errors"]]
        body = intro + '<div class="empty"><h2>尚未生成论文总结</h2><p>' + escape(message) + '</p></div>'
        return "\n".join(lines), page("推荐研究日报 · " + day, body)
    if payload["errors"]:
        intro += '<aside class="notice">本期部分任务未完成，错误和待重试论文已保留；以下仅展示已完成的全文解读。</aside>'
    trends = []
    for trend in payload["synthesis"]["trends"]:
        lines += [f"### {md(trend['title'])}", "", md(trend["analysis"]), "",
                  "依据：" + "、".join(md(i) for i in trend["paper_ids"]), ""]
        trends.append(f'<article class="trend"><h3>{escape(trend["title"])}</h3><p>{escape(trend["analysis"])}</p><div class="sources">{source_links(trend["paper_ids"], by_id)}</div></article>')
    if not trends:
        lines += ["本期没有足够的跨论文证据生成趋势判断。", ""]
        trends.append('<p class="muted">本期没有足够的跨论文证据生成趋势判断。</p>')
    overview = []
    lines += ["## 今日速览", ""]
    for item in items:
        paper, analysis = item["paper"], item["insight"]["analysis"]
        lines += [f"- [{md(analysis['title_zh'])}]({paper['url']})：{md(analysis['one_liner'])}"]
        overview.append(f'<li><a href="#{anchor(paper["arxiv_id"])}">{escape(analysis["title_zh"])}</a><span>{escape(analysis["one_liner"])}</span></li>')
    lines += ["", "## 重点论文 · 全文 insight", ""]
    cards = []
    for item in items:
        paper, analysis = item["paper"], item["insight"]["analysis"]
        source = {"organizations": item["companies"], "organization_basis": item["company_basis"]}
        organizations = source["organizations"]
        organization_text = " / ".join(organizations) or "机构待核验"
        paper_date = (paper.get("updated") or "未知")[:10]
        badges = "".join(f'<span class="pill">{escape(name)}</span>' for name in (organizations or ["机构待核验"]))
        evidence = {e["id"]: e for e in analysis["evidence"]}
        lines += [f"### {md(analysis['title_zh'])}", "", f"[{md(paper['title'])}]({paper['url']})", "",
                  f"机构：{md(organization_text)}（{md(source['organization_basis'])}）；阅读优先级：{analysis['rating']}/5。", "",
                  "论文更新日期：" + md(paper_date) + "（UTC）。", "",
                  "推荐理由：" + md(analysis["recommendation_reason"]), "",
                  "**研究问题**：" + md(analysis["problem"]), "", "**核心方法**：" + md(analysis["method"]), "",
                  "**实验结果**", ""]
        results, insights, limitations = [], [], []
        for result in analysis["key_results"]:
            ref = result["evidence_id"]
            lines.append(f"- {md(result['finding'])} [{md(ref)}]")
            results.append(f'<li>{escape(result["finding"])}</li>')
        if not results:
            lines += ["未提取到可引用的实验结果。"]
        lines += ["", "**工程 insight（分析推断）**", ""]
        for insight in analysis["insights"]:
            lines.append("- " + md(insight["idea"]) + " [" + ", ".join(md(r) for r in insight["evidence_ids"]) + "]")
            insights.append(f'<li>{escape(insight["idea"])}</li>')
        lines += ["", "**局限与阅读提醒**", ""]
        for limitation in analysis["limitations"]:
            lines.append("- " + md(limitation))
            limitations.append('<li>' + escape(limitation) + '</li>')
        lines += ["", "**证据定位**", ""]
        for key, ev in evidence.items():
            lines.append(f"- {md(key)} · {md(ev['block_id'])}：{md(ev['quote'])}")
        lines += ["", "提取范围：" + item["insight"]["extraction"]["note"], ""]
        search_text = " ".join([paper["title"], analysis["title_zh"], organization_text,
                                *item["companies"], *item["selection"]["tags"]])
        reading_link = ""
        if paper["arxiv_id"] in (paper_links or {}):
            reading_link = f'<a class="reading-link" href="{escape(paper_links[paper["arxiv_id"]])}">阅读中文全文 →</a>'
        cards.append(f'''<article class="paper" id="{anchor(paper["arxiv_id"])}" data-search="{escape(search_text)}">
<div class="paper-meta">{badges}<span>阅读优先级 {'★' * analysis['rating']}{'☆' * (5 - analysis['rating'])}</span></div>
<h3>{escape(analysis['title_zh'])}</h3><a class="original" href="{escape(paper['url'])}" target="_blank" rel="noopener noreferrer">{escape(paper['title'])} ↗</a>
{reading_link}
<p class="muted">{escape(source['organization_basis'])} · 论文更新于 {escape(paper_date)}（UTC）</p><p class="reason">{escape(analysis['recommendation_reason'])}</p>
<h4>研究问题</h4><p>{escape(analysis['problem'])}</p><h4>核心方法</h4><p>{escape(analysis['method'])}</p>
<h4>实验结果</h4><ul>{''.join(results) or '<li>未提取到可引用的实验结果。</li>'}</ul>
<div class="insight"><h4>工程 insight <small>分析推断</small></h4><ul>{''.join(insights)}</ul></div>
<h4>局限与阅读提醒</h4><ul>{''.join(limitations)}</ul></article>''')
    topic = payload["synthesis"].get("topic")
    topic_html = '<p class="muted">专题暂未生成。</p>'
    lines += ["## 今日专题", ""]
    if topic:
        lines += [f"### {md(topic['title'])}", "", md(topic["analysis"]), "", "依据：" + "、".join(md(i) for i in topic["paper_ids"]), ""]
        topic_html = f'<div class="topic"><h3>{escape(topic["title"])}</h3><p>{escape(topic["analysis"])}</p><div class="sources">{source_links(topic["paper_ids"], by_id)}</div></div>'
    if payload["errors"]:
        lines += ["## 待重试", ""] + [f"- {e['arxiv_id']}：{md(e['error'])}" for e in payload["errors"]]
    body = intro + '<nav><a href="#trends">样本观察</a><a href="#briefs">今日速览</a><a href="#digest">全文解读</a><a href="#topic">今日专题</a></nav>'
    body += '<section id="trends"><h2><span class="no">01</span>当日样本观察</h2><div class="trends">' + ''.join(trends) + '</div></section>'
    body += '<section id="briefs"><h2><span class="no">02</span>今日速览</h2><ol class="briefs">' + ''.join(overview) + '</ol></section>'
    body += '<section id="digest"><h2><span class="no">03</span>重点论文 · 全文 insight</h2><input id="search" type="search" placeholder="筛选标题、机构、标签…" aria-label="筛选论文">' + ''.join(cards) + '</section>'
    body += '<section id="topic"><h2><span class="no">04</span>今日专题</h2>' + topic_html + '</section>'
    return "\n".join(lines), page("推荐研究日报 · " + day, body)


def read_paper_catalog(directory):
    """Return only built local paper pages; a draft must not get a reading link."""
    directory = Path(directory)
    catalog = directory / "papers" / "catalog.json"
    if not catalog.is_file():
        return []
    try:
        records = json.loads(catalog.read_text(encoding="utf-8"))["papers"]
    except (OSError, ValueError, KeyError, TypeError):
        logging.getLogger(__name__).warning("无法读取论文目录：%s", catalog, exc_info=True)
        return []
    papers = []
    for record in records:
        url = record.get("url", "")
        path = Path(url)
        if (not url.startswith("papers/") or ".." in path.parts or path.suffix != ".html"
                or not (directory / path).is_file()):
            continue
        papers.append(record)
    return papers


def edition_cards(editions, prefix=""):
    cards = []
    for edition in editions:
        titles = " / ".join(item["insight"]["analysis"]["title_zh"] for item in edition["items"][:3])
        teams = sorted({name for item in edition["items"] for name in item["companies"]})
        search_text = " ".join([item["paper"]["title"] + " " + item["insight"]["analysis"]["title_zh"]
                                + " " + " ".join(item["companies"]) for item in edition["items"]] + teams)
        team_summary = " · ".join(teams[:4]) + (f" 等 {len(teams)} 家机构" if len(teams) > 4 else "")
        cards.append(f'<a class="edition" href="{escape(prefix + edition["date"])}.html" data-search="{escape(search_text)}"><span class="eyebrow">{escape(edition["date"])}</span><h2>{len(edition["items"])} 篇全文解读</h2><p>{escape(titles)}</p><span class="muted">{escape(team_summary)} · {escape(edition["status"])}</span></a>')
    return cards


def publish_index(connection, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if connection is None:
        editions = [json.loads(path.read_text(encoding="utf-8"))
                    for path in sorted((directory.parent / "daily").glob("????-??-??.json"), reverse=True)]
    else:
        editions = [json.loads(row[0]) for row in connection.execute("SELECT payload_json FROM editions ORDER BY day DESC")]
    archive_dir = directory / "daily"
    archive_dir.mkdir(exist_ok=True)
    empty = '<div class="empty"><h2>从论文原文，到值得读的日报。</h2><p>配置 DeepSeek、Kimi 或 GLM 官方 API 后运行 daily。</p><p>系统会优先关注推荐与检索的大团队工作，读取全文并生成结构化中文解读。</p><div class="steps">发现论文 → 全文提取 → Insight → 样本观察 → 日报归档</div><p class="muted">当前没有已生成的日报；这里没有填充虚构论文或模型结论。</p></div>'
    body = '<p class="lede">追踪推荐与检索的研究进展，理解方法背后的取舍。</p><input id="search" type="search" placeholder="按论文标题、机构查找日报…" aria-label="搜索历史日报">'
    body += '<div class="archive">' + (''.join(edition_cards(editions, "../")) if editions else empty) + '</div>'
    atomic_write(archive_dir / "index.html", page("研究日报", body, "DAILY RESEARCH / 历史归档", root_prefix="../"))

    papers = read_paper_catalog(directory)
    paper_index = directory / "papers" / "index.html"
    if not paper_index.is_file():
        paper_index.parent.mkdir(exist_ok=True)
        atomic_write(paper_index, page("论文阅读", '<p class="lede">论文全文正在整理中。</p>',
                                     "PAPER LIBRARY / 中文全文", root_prefix="../", section="papers"))
    paper_cards = []
    for paper in papers[:3]:
        tags = " · ".join(paper.get("topics", []))
        paper_cards.append(f'<a class="edition" href="{escape(paper["url"])}"><span class="eyebrow">{escape(paper["short_name"])}</span><h3>{escape(paper["title_zh"])}</h3><span class="original">{escape(paper["title_en"])}</span><p>{escape(tags)}</p><span class="reading-link">阅读中文全文 →</span></a>')
    paper_empty = '<p class="muted">论文全文正在整理中。</p>'
    home = '<p class="lede">追踪推荐与检索的研究进展，阅读方法背后的完整论证。</p>'
    home += '<section><div class="section-heading"><h2>最新日报</h2><a href="daily/index.html">全部日报 →</a></div>'
    home += (''.join(edition_cards(editions[:1])) if editions else empty) + '</section>'
    home += '<section><div class="section-heading"><h2>论文阅读</h2><a href="papers/index.html">全部论文 →</a></div><div class="library-preview">'
    home += (''.join(paper_cards) if paper_cards else paper_empty) + '</div></section>'
    atomic_write(directory / "index.html", page("推荐系统研究阅读", home, "RECSYS RESEARCH / 日报与论文", section="home"))
    return str(directory / "index.html")


def publish_daily(payload, output_dir, connection, update_index=True):
    output_dir = Path(output_dir)
    daily_dir, site_dir = output_dir / "daily", output_dir / "site"
    daily_dir.mkdir(parents=True, exist_ok=True)
    site_dir.mkdir(parents=True, exist_ok=True)
    paper_links = {paper["arxiv_id"]: paper["url"] for paper in read_paper_catalog(site_dir)}
    markdown, webpage = render_daily(payload, paper_links=paper_links)
    stem = daily_dir / payload["date"]
    atomic_write(stem.with_suffix(".json"), json.dumps(payload, ensure_ascii=False, indent=2))
    atomic_write(stem.with_suffix(".md"), markdown)
    atomic_write(site_dir / (payload["date"] + ".html"), webpage)
    index = publish_index(connection, site_dir) if update_index else None
    return {"markdown": str(stem.with_suffix(".md")), "json": str(stem.with_suffix(".json")),
            "html": str(site_dir / (payload["date"] + ".html")), "archive": index}


def rebuild_reports(connection, output_dir, config):
    """Refresh saved editions and organization display without calling a model."""
    editions = [json.loads(row[0]) for row in connection.execute("SELECT payload_json FROM editions ORDER BY day")]
    with connection:
        for payload in editions:
            for item in payload["items"]:
                item.update(organization_details(item["paper"], item["insight"]["analysis"], config))
                item["companies"] = item["organizations"]
                item["company_basis"] = item["organization_basis"]
                item["preferred_companies"] = sorted(set(item["companies"]) & set(config["preferred_companies"]))
            connection.execute("UPDATE editions SET payload_json=? WHERE day=?",
                               (json.dumps(payload, ensure_ascii=False), payload["date"]))
            publish_daily(payload, output_dir, connection, update_index=False)
    return {"editions_rebuilt": len(editions), "archive": publish_index(connection, Path(output_dir) / "site")}
