# 深清 DeepClean

**C 盘被 AI 工具吃满了？** 专治 Cursor / Claude Code / Codex / Hugging Face / Ollama / 豆包 / Kimi 把 C 盘吃满。

[EN](#english) | 中文

- **按工具展示，不是按文件类型**：Cursor 占了多少、Ollama 的模型能不能迁走，一眼看清
- **四档安全级**：安全可删（绿）/ 可重建（琥珀）/ 建议迁移（蓝）/ 危险·锁定（红）—— 会话记录与对话历史被硬性锁定，**永不清理**
- **三种清理模式**：安全一键（推荐）/ AI 工作站 / 专家
- **模型只统计与预览**：模型不参与清理；当前迁移仅预览第一个明确模型目录，不复制、不删源
- **规则开源可审计**：全部路径、安全级、理由写在 [`rules/`](rules/) 目录，欢迎 PR 补充工具
- **纯本地**：只监听 127.0.0.1，不联网、无遥测；Python 标准库实现，零第三方依赖

## 安全承诺

1. 默认只读扫描，不自动删除
2. 会话与文件历史（Codex `sessions`、Claude `projects` 与 `file-history`、ZCode `rollout`、Cursor 对话历史与 `User\History`）**永不进入清理计划**（后端硬排除）；WinSxS / 虚拟内存 / 休眠文件 / 系统还原点等系统危险项同样锁定，请用系统自带工具处理
3. 模型文件不删除，当前版本仅提供迁移预览；管理员实例只允许只读扫描和预览
4. 保留期内的文件、被占用文件自动跳过
5. 清理只删除扫描时记录的、属于规则路径的文件
6. 普通清理请求 Windows Shell 回收文件，失败不回退强删；Shell 的回收能力取决于介质和系统设置，不能保证所有环境均可恢复。处理的逻辑大小不等于磁盘释放空间

## 下载与使用

| 方式 | 说明 |
|---|---|
| 源码运行 | `python app.py`，或双击 `启动.bat`（管理员启动仅可只读扫描）。单文件 exe 正在打包验证，随后发布到 Releases |

打包 exe：`打包exe.bat`（需要 Python + pip 网络）。

### 命令行（供 AI 助手 / 脚本）

```bash
python app.py cli scan                                  # 扫描：各工具/分项大小 + 目录明细
python app.py cli scan --ids npm-store,python-cache      # 只扫描指定分项（id 请以 categories 输出为准）
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
- [x] 运行中应用清理前提醒
- [ ] zh-TW / ja 翻译

<a id="english"></a>

# DeepClean (English)

**Did AI tools eat your C: drive?** Purpose-built to reclaim space from Cursor, Claude Code, Codex, Hugging Face, Ollama, Doubao, Kimi and friends.

- **Organized by tool, not file type** — see what Cursor ate and whether your Ollama models can move
- **Four safety levels** — safe (green) / rebuildable (amber) / migrate (blue) / danger·locked (red). Chat history is hard-locked and **never cleaned**
- **Three cleanup modes** — Safe clean (recommended) / AI workstation / Expert
- **Model statistics and migration preview** — no source deletion or copying in this release. Elevated instances are read-only.
- **Auditable open rules** — every path, risk level and reason lives in [`rules/`](rules/)
- **Local only** — listens on 127.0.0.1, no network, no telemetry; pure Python stdlib

```bash
python app.py cli scan
python app.py cli clean --ids npm-store --yes
python app.py cli move --tool ollama --to D --dry
```

See [CONTRIBUTING.md](CONTRIBUTING.md) to add rules for your favorite tool. License: [MIT](LICENSE).

## 本轮完善与验收

任务及后续版本边界见 [任务清单](docs/TASKS.md)。本地 API 需要每次启动的会话凭据；请从启动程序自动打开的页面使用，复制不带凭据的地址不能访问扫描详情。网页确认绑定五分钟有效、一次使用的后端计划。清理后必须重新扫描。

测试只使用临时目录：

```text
python tests/test_rules.py
python tests/test_http.py
python tests/test_recycle.py
python tests/test_safety.py
python tests/test_sandbox.py
python tests/test_lifecycle.py
```

`CLEAR_C_SANDBOX` 仅供测试：扫描仅限测试类别，永久删除也必须位于该目录且路径无重解析点。已移除 `DEEPCLEAN_PERMANENT` 和非 Windows 自动永久删除回退。请勿在正常启动中设置测试变量。

### 启动、退出与扫描范围

同一安装目录、同一用户和权限模式下只保留一个实例；重复启动会打开已有实例。普通权限与管理员只读实例分别协调。CLI 与同一模式的网页实例互斥，使用 CLI 前先退出网页实例。

在设置中点击“退出工具”，或运行 `退出工具.bat`。任务执行期间先停止并等待结束，程序不会强杀进程。启动器不再靠固定端口判断启动成功，实例信息使用 Windows DPAPI 加密存放在本地用户目录。

界面只扫描当前分类，每个工具都有“只扫描此工具”入口；CLI clean 自动只扫描指定分项。未扫描分项明确标注，不加入清理计划。清单最多保留 150,000 条，并采用 96 MiB 的保守估算预算；这不是进程 RSS 的硬限制。达到任一上限后仍可看统计，但必须缩小范围重扫才能清理。

### EXE 候选包

`打包exe.bat` 在隔离构建环境安装 `requirements-build.txt` 的固定依赖，输出到 `dist/candidate/`，附带 `SHA256SUMS.txt` 和 `build-manifest.json`。后者记录 Python、平台和源文件摘要，方便核对构建输入，不宣称跨机器逐字节一致。候选包未签名、未自动上传；使用截图与演示素材前请参阅 [推广素材包](docs/promotion/README.md)。

## 支持作者与最低成本推广

项目免费使用，支持作者完全自愿，不影响功能使用。设置和清理结果中提供微信赞赏、支付宝二维码，以及 [♥ GitHub Sponsors](https://github.com/sponsors/onesun2012)、[Ko-fi](https://ko-fi.com/onesun)、[爱发电](https://afdian.com/a/onesun)。二维码从作者的 `ai_desk` 项目原样复制，点击对应入口展开；可点击图片查看原图。

入口由根目录 `support.json` 配置，支持 HTTPS `url` 或两张内置二维码的 `image` 路径，最多五项；空配置自动隐藏。配置和图片均随打包分发，清理结果的支持区域仍可选择不再显示。

先修复与小范围测试，再录制一个 30–60 秒演示，在项目 README、Release 和已有开发者社区分享同一份内容。优先统计反馈、使用障碍与自愿支持情况，现金预算先按零元，不投广告、不建收费后台。具体四周方案见 [代码审查与增长方案](docs/REVIEW-2026-09-06.md)。
