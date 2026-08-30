# 安全策略（Security Policy）

深清 DeepClean 是一个删除文件的工具，安全是我们的第一优先级。如果你发现任何**误删**、**路径错误**或**安全问题**，请按以下方式报告。

## 报告误删（Wrongly deleted files）

1. 在 GitHub 提 Issue，选择「误删」模板，包含：
   - 被删除的目录/文件路径（用户名可打码）
   - 使用的模式（安全一键 / AI 工作站 / 专家）与勾选的分项 id
   - 清理发生的大致时间
2. 我们会：
   - 立即下线有问题的规则（rules/ 对应条目）
   - 在下个版本中修复并在 Release Note 中说明

## 报告安全漏洞

请不要通过公开 Issue 报告安全漏洞（如任意路径删除、提权风险）。请使用 GitHub Security Advisories 或联系仓库所有者。

## 永不清理清单（后端硬排除，任何入口都无法触发）

- 全部 AI 工具的会话记录：`~/.codex/sessions`、`~/.claude/projects`、`~/.zcode/cli/rollout`、Cursor 对话历史（`globalStorage`）等
- 文件历史与回滚快照：`~/.claude/file-history`、Cursor `User\History`
- 系统级危险项：WinSxS / 虚拟内存 / 休眠文件 / 系统还原点（全部 `locked`，如需处理请用系统自带工具）
- `Windows.old` 等系统回滚数据不在任何规则中

## 安全设计承诺

- 默认只读扫描，不自动删除任何文件
- `locked`（danger）分项在后端被硬性排除，任何入口（网页/CLI/skill）都无法清理（清单见上）
- 未锁定的危险分项必须显式确认才会进入清理计划：网页请求需 `confirm_danger`，CLI 需 `--confirm-danger`
- `migrate` 分项（本地模型）不进入清理计划，只能通过迁移通道（复制 → 校验 → junction → 删源，失败回滚）
- 回收站分项为「可重建」级，三种模式均默认不勾选（清空后无法再从回收站找回文件）
- 网页服务只接受 Host/Origin 指向本机且端口一致的请求，拒绝跨站 Origin / `Sec-Fetch-Site: cross-site` 与 DNS rebinding
- 清理只删除「扫描阶段记录下来的、属于规则 paths」的文件，不接受任意路径
- 保留期（min_age_min）内的文件自动跳过；被占用/无权限的文件自动跳过
- 迁移复制使用 `symlinks=True`，保留 Hugging Face 等缓存内的链接结构
- 工具只监听 127.0.0.1，不联网、无遥测

## 迁移中断的手动恢复

迁移流程任何一步失败都会自动回滚；唯一无法自动处理的是**进程在迁移中途被强杀/断电**：
若源目录旁出现 `xxx.deepclean-bak` 且原位置已无目录联接，把该目录改回原名（去掉 `.deepclean-bak` 后缀）即完全恢复；若目录联接已建好且数据完整，删除 `.deepclean-bak` 即可。

## 规则审计

所有清理路径、安全级与理由都写在 [`rules/`](rules/) 目录，逐条可审计；`tests/test_rules.py` 会拦截「会话路径出现在可清理分项」等规则回退。欢迎通过 PR 修正或补充规则。
