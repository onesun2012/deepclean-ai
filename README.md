# 深清 DeepClean

**C 盘被 AI 工具吃满了？** 专治 Cursor / Claude Code / Codex / Hugging Face / Ollama / 豆包 / Kimi 把 C 盘吃满。

[EN](#english) | 中文

- **按工具展示，不是按文件类型**：Cursor 占了多少、Ollama 的模型能不能迁走，一眼看清
- **四档安全级**：安全可删（绿）/ 可重建（琥珀）/ 建议迁移（蓝）/ 危险·锁定（红）—— 会话记录与对话历史被硬性锁定，**永不清理**
- **三种清理模式**：安全一键（推荐）/ AI 工作站 / 专家
- **模型迁移而不是删除**：Hugging Face / Ollama / LM Studio 一键剪切到 D 盘并保留目录联接，应用零配置继续可用
- **规则开源可审计**：全部路径、安全级、理由写在 [`rules/`](rules/) 目录，欢迎 PR 补充工具
- **纯本地**：只监听 127.0.0.1，不联网、无遥测；Python 标准库实现，零第三方依赖

## 安全承诺

1. 默认只读扫描，不自动删除
2. 会话记录（Codex `sessions`、Claude `projects`、Cursor 对话历史）**永不进入清理计划**（后端硬排除）
3. 模型文件不删除，只提供「迁移 + 目录联接」
4. 保留期内的文件、被占用文件自动跳过
5. 清理只删除扫描时记录的、属于规则路径的文件

## 下载与使用

| 方式 | 说明 |
|---|---|
| `DeepClean.exe`（Releases） | 单文件，免安装 Python，双击即用 |
| 源码运行 | `python app.py`，或双击 `启动.bat`（管理员用 `以管理员启动.bat`） |

打包 exe：`打包exe.bat`（需要 Python + pip 网络）。

### 命令行（供 AI 助手 / 脚本）

```bash
python app.py cli scan                                  # 扫描：各工具/分项大小 + 目录明细
python app.py cli categories                            # 全部分项与安全级
python app.py cli clean --ids npm-store,codex-cache     # 输出清理计划（退出码 2 = 待确认）
python app.py cli clean --ids npm-store --yes           # 执行清理（locked/migrate 自动跳过）
python app.py cli move --tool ollama --to D --dry       # 预览迁移 Ollama 模型到 D 盘
```

封装为 AI 技能：`python install_skill.py`（安装到 `~/.agents/skills`、`~/.claude/skills`、`~/.codex/skills`）。

## 支持的 AI 工具（节选）

Cursor · Claude Code · ZCode · Codex · GitHub Copilot · Windsurf · Gemini CLI · OpenClaw · Trae · Qoder · CodeBuddy ·
Hugging Face · Ollama · PyTorch/CUDA · LM Studio · 豆包 · Kimi · 扣子 · 腾讯 ima · 夸克 ·
npm/pnpm/Yarn · Docker WSL · pip/uv · JetBrains · Playwright · Electron · NuGet · 微信 · QQ/企业微信

完整清单见 [rules/](rules/)。

## Roadmap

- [ ] Docker WSL vhdx 压缩/迁移向导
- [ ] 删除进隔离区（7 天可还原）+ 操作历史
- [ ] 定时只扫不删 + 托盘提示
- [ ] 运行中应用清理前提醒（已检测进程，提示待加）
- [ ] zh-TW / ja 翻译

<a id="english"></a>

# DeepClean (English)

**Did AI tools eat your C: drive?** Purpose-built to reclaim space from Cursor, Claude Code, Codex, Hugging Face, Ollama, Doubao, Kimi and friends.

- **Organized by tool, not file type** — see what Cursor ate and whether your Ollama models can move
- **Four safety levels** — safe (green) / rebuildable (amber) / migrate (blue) / danger·locked (red). Chat history is hard-locked and **never cleaned**
- **Three cleanup modes** — Safe clean (recommended) / AI workstation / Expert
- **Move models, don't delete** — one-click relocate HF/Ollama/LM Studio to another drive with a junction; apps keep working with zero config
- **Auditable open rules** — every path, risk level and reason lives in [`rules/`](rules/)
- **Local only** — listens on 127.0.0.1, no network, no telemetry; pure Python stdlib

```bash
python app.py cli scan
python app.py cli clean --ids npm-store --yes
python app.py cli move --tool ollama --to D --dry
```

See [CONTRIBUTING.md](CONTRIBUTING.md) to add rules for your favorite tool. License: [MIT](LICENSE).
