---
name: clear-c
description: 深清 DeepClean — C 盘清理工具，专治 AI 工具吃满 C 盘。当用户要求清理C盘、释放磁盘空间、清理 Cursor/Claude Code/Codex/ZCode 缓存、清理 npm/pip/uv 缓存、清理 HuggingFace/Ollama 本地模型（支持迁移到 D 盘）、清理豆包/Kimi/微信/QQ 缓存时使用。先扫描预览再确认清理，全部本地执行。
---

# 深清 DeepClean（C 盘 AI 工具专清）

本机已安装「深清 DeepClean」，工具本体位于 `{{APP_DIR}}`。通过其 CLI 子命令扫描并分类清理，stdout 输出 JSON，进度走 stderr。

## 核心概念

- **工具（tool）→ 分项（bucket）**：界面与规则按工具聚合（如 Cursor），每个工具下有多个分项（应用缓存/代码索引/会话历史…），分项是勾选与清理的最小单元
- **四档安全级（risk）**：
  - `safe` 安全可删（日志/临时/更新包）—— 可直接清理
  - `rebuildable` 可重建（包缓存/索引）—— 可清理，删除后下次使用会重建
  - `migrate` 建议迁移（本地模型等大文件）—— **不可删除**，用 `cli move` 迁移到其他盘
  - `danger` 危险（会话记录/对话历史）—— **锁定，永不清理**，任何命令都会被后端拒绝

## 可用命令

```bash
# 1. 全部分项与安全级
python "{{APP_DIR}}\app.py" cli categories

# 2. 扫描（输出 tools/buckets/totals：safe+review+migrate 大小、每分项目录明细）
python "{{APP_DIR}}\app.py" cli scan

# 3. 清理计划（不删除；退出码 2 = 待确认）
python "{{APP_DIR}}\app.py" cli clean --ids npm-store,codex-cache

# 4. 执行清理（locked/migrate 分项自动跳过并在结果中列出）
python "{{APP_DIR}}\app.py" cli clean --ids npm-store,codex-cache --yes

# 5. 按目录排除（分项内只想清部分目录时）
python "{{APP_DIR}}\app.py" cli clean --ids cursor-cache --yes --exclude-root "C:\Users\<用户>\AppData\Roaming\Cursor\Cache\Code Cache"

# 6. 迁移本地模型到其他盘（先 dry 预览）
python "{{APP_DIR}}\app.py" cli move --tool ollama --to D --dry
python "{{APP_DIR}}\app.py" cli move --tool ollama --to D
```

- `cli scan` 输出结构：`tools[]`（含 running/last_used_days/size）、`buckets[]`（含 risk/locked/cleanable/size/roots 目录明细）、`totals{safe,review,migrate}`、`drive{free,total}`
- `cli clean` 需要时会自动先扫描；每分项支持 `--exclude-root` 排除子目录

## 标准工作流程

1. **摸底**：`cli scan`，把结果整理成「工具 → 大小 → 可清/可迁」清单展示给用户
2. **确认**：推荐先清 `safe` 级；`rebuildable` 级说明"删除后下次使用会重建"；`migrate` 级建议迁移而非删除。**等待用户确认**
3. **执行**：`cli clean --ids ... --yes`，汇报 freed/skipped 与各分项 note
4. **迁移**：用户同意后 `cli move --tool <id> --to <盘符>`（务必先 `--dry` 预览）；迁移完成报告"原位置已建立目录联接，应用无需配置"

## 安全红线（必须遵守）

- `danger`/`locked` 分项（全部 AI 工具的会话记录）**永远不会被清理**——不要请求，后端也会拒绝
- `migrate` 分项不可清理，只能走 `cli move`；迁移前必须先 `--dry` 并征得用户同意
- 系统级分项（`winsxs`/`pagefile`/`hibernate`/`restore-points`）执行系统命令，必须用户明确同意
- 只通过本工具 CLI 清理，不要绕过工具手动删除任何路径
- 工具仅监听本机、不联网；扫描结果只含路径/大小/时间，可放心展示
