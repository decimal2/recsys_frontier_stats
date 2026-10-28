# 用 harness 维护论文网站

这里的两个 skill 将模型与工具编排交给 Codex、Claude Code 等支持本地 skill 的 harness。它们直接检索、阅读全文、写作和维护静态站点，**不运行 `pipeline.py`**，也不要求另配 DeepSeek / Kimi / GLM 密钥；使用的是你的 harness 已有模型和联网工具及其额度。

| Skill | 用途 | 入口 |
|---|---|---|
| `recsys-daily` | 检索工业界论文、全文 insight、日报与归档、发布 | [SKILL.md](recsys-daily/SKILL.md) |
| `recsys-reading` | 全文中文 HTML、论文目录与阅读界面、排版校验、发布 | [SKILL.md](recsys-reading/SKILL.md) |

## 克隆后使用

在仓库根目录启动 harness，读取根目录 `AGENTS.md`。技能路径与仓库路径分开处理，不能写死原作者的目录或 GitHub 地址。

支持直接读取文件的 harness 可以这样使用，无需安装：

> 请读取 skills/recsys-daily/SKILL.md，使用你的联网工具收集今天的推荐系统日报，5–10 篇工业界论文。先生成 docs 页面，不发布。

> 请读取 skills/recsys-reading/SKILL.md，为 arXiv:<论文ID> 制作中文全文 HTML，加入我的论文阅读专栏并发布到当前仓库的 GitHub Pages。

## 注册为本地 skill

不同 harness 的发现规则可能不同，按其说明注册 `SKILL.md`。Codex 可将技能目录链接到 `~/.codex/skills/`，Claude Code 可使用 `~/.claude/skills/`。以下在克隆仓库根目录执行，仅在目标链接尚不存在时创建：

```bash
# Codex；Claude Code 将这一行改为 skills_root="$HOME/.claude/skills"
skills_root="${CODEX_HOME:-$HOME/.codex}/skills"
mkdir -p "$skills_root"
for name in recsys-daily recsys-reading; do
  if [ ! -e "$skills_root/$name" ] && [ ! -L "$skills_root/$name" ]; then
    ln -s "$PWD/skills/$name" "$skills_root/$name"
  fi
done
```

重新载入 harness 后，Codex 可显式调用 `$recsys-daily` / `$recsys-reading`；Claude Code 按其 skill 调用方式使用。引用文件位于仓库 `skills/`，运行时先解析符号链接，再按实际技能源目录解析；若 harness 不跟随链接，直接读取仓库中的入口与引用文件。不要只拷贝一个 SKILL.md；两个目录共用 [发布指南](references/publishing.md)，移动安装时需保留相对目录结构或调整引用。

## 使用示例

```text
使用 $recsys-daily 收集 2026-10-29 的日报，最多 10 篇。
优先大公司推荐系统工作，不足时回溯 7、14、30 天；生成并发布到我的仓库。

使用 $recsys-daily 发布已有的今天日报，不重新检索或调用独立模型 API。

使用 $recsys-reading 翻译 arXiv:<ID> 的固定版本，保留附录、公式、图表和参考文献，
加入“生成式推荐”主题，检查手机与桌面排版后发布。

使用 $recsys-reading 调整我现有阅读库的分类和首页，保留已有全文与日报。
```

## 环境与发布

- 检索需要 harness 的搜索、抓取或浏览器能力；只有聊天而无法读写文件、联网的环境不能完成整个流程。
- 日报可用 Python 标准库复用离线渲染器。论文页面包装需 `beautifulsoup4`；新增 TeX 全文需 LaTeXML，视觉检查需 Node.js、Chrome 和字体。无需启动完整 pipeline，但转换与校验工具仍需要安装。
- 全文与日报共存于 `docs/`，克隆后即有静态快照。`reports/`、数据库、缓存和本地 PDF 不随 Git 克隆；skill 必须保留已有归档，不能按空数据库重建覆盖。
- 发布需要你自己的 GitHub 写权限与 Pages 配置。目标从当前 remote 推导；fork 不应推送原作者仓库。未要求发布时只生成本地文件。
- 新增论文仅发布 HTML 与必要图形，PDF 可按需求留在被忽略的 `paper_pdfs/`。日志写入实际日期的 `logs/YYYY-MM-DD/HH-MM-SS.log`。
- 发布完成以线上文件校验为准；自动检查保留在 `.github/workflows/`。仅推送成功不等于页面已经更新。

这两个 skill 是可执行工作指导，仓库本身不会因此自动定时采集。需要无人值守时，在自己的 harness 中配置任务调度与工具权限。
