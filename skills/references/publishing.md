# 两个专栏共用的静态发布流程

本文件相对 `skills/`，供两个 skill 在准备发布时读取。默认直接维护 `docs/`，不运行 pipeline，也不依赖 `reports/` 历史数据。

## 目标、授权与差异

从当前 Git remote 与分支确定目标，不沿用 decimal2 的地址。对于普通项目 Pages，地址通常为 `https://<owner>.github.io/<repo>/`；个人主页仓库用根路径，自定义域名用 Pages 的真实配置。用户克隆但不是仓库所有者时，发布到其有权限的 fork；没有写权限先交付可审阅结果，再说明具体缺失配置。

读取 Git 状态，保留用户修改。生成与离线检查可直接完成；用户要求发布即授权该任务的提交推送。只要求本地生成时不要擅自推送、创建远端仓库或启用 Pages。

## 发布前检查

1. 对照 Git diff 确认本次页面、双专栏导航、历史归档和图形齐全，存在 `docs/.nojekyll`。只允许静态页面与必要资源；拒绝密钥、日志、PDF、源码下载包和缓存。
2. 解析 HTML 的相对 href/src，验证本地目标和片段 ID 存在；正确处理 URL 编码、目录链接及 Pages 项目前缀。检查新页面手机/桌面布局；论文全文运行浏览器审计并查看截图。
3. `git diff --check`。只 stage 本次明确文件，避免 `git add .` 混入用户修改。论文目录源清单、必要渲染代码一并提交；日报原始 JSON 留在被忽略的 `reports/daily/`。

## 提交与推送

优先使用 harness 的 GitHub 连接器；否则使用已认证 Git。不要读取或打印凭据。CLI 示例（路径按本次实际文件填写）：

```bash
git add -- docs/YYYY-MM-DD.html docs/daily/index.html docs/index.html
git diff --cached --check
git diff --cached --stat
git commit -m "publishing: publish YYYY-MM-DD research digest"
git push origin HEAD
```

先核对本地和远端 HEAD；分支落后时正常获取并整合已授权变更，不能强推或丢弃他人提交。通过连接器提交时，同样检查远端预期 HEAD，并确保最后本地工作树、索引和分支状态准确；不能仅修改远端后宣称本地已同步。

`bash scripts/publish.sh` 是可选替代，只适用于 `reports/site/` 已经是本次完整站点。它不会同步本地 `docs/`，且默认指向原作者仓库；fork 使用时明确配置 `PUBLISH_REPO_URL`、`PUBLISH_BRANCH`、`PUBLISH_SITE_URL`。不要用陈旧 reports/site 覆盖刚生成的 docs。

## Pages 与线上验收

首次发布需要目标仓库 Pages 设置为分支部署 `main /docs`（或用户实际分支），保留 `.github/workflows/` 自动检查。更换默认分支时核对工作流触发分支。没有 Pages 管理权限时给出设置路径，不能宣称页面已上线。

等待 `pages build and deployment` 和 `Verify published site` 完成。正文或阅读界面变化还需检查 `Paper rendering`；失败时读日志、修复、再正常提交，不能为通过而关闭检查。自定义域名设置 Actions 变量 `PAGES_SITE_URL`。

可在有网络的本地使用标准库验证整个发布快照：

```bash
python3 scripts/verify_pages.py --site-dir docs \
  --site-url https://OWNER.github.io/REPOSITORY/ \
  --report /tmp/pages-verification.json
```

要求本次页面 HTTP 200、正文和资源哈希与提交一致，检查首页入口和归档。重试有截止时间，默认最多等待 600 秒；超时保留提交与错误日志，不反复推送相同内容。最终区分“本地已生成”“已推送”“线上验证成功”，给出实际链接与未完成检查。
