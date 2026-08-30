# 贡献指南（Contributing to DeepClean）

感谢关注深清！最有价值的贡献是**补充清理规则** —— 每一条新规则都直接帮助和你用同样工具的人。

## 补充一个新工具规则（最欢迎）

1. 先开一个 Issue 用「新工具规则」模板，写清楚：工具名、占用的目录、你实测的大小
2. 修改 `rules/` 下对应的 JSON（AI 工具进 `ai.json`，本地模型进 `models.json`，以此类推）：
   - `tools` 数组加一条：`{"id": "mytool", "name": "MyTool", "category": "assistant", "processes": ["mytool.exe"]}`
   - `buckets` 数组加分项，**每个分项必须填写**：
     - `risk`：`safe`（日志/临时）/ `rebuildable`（缓存/索引，删后重建）/ `migrate`（模型等大文件，只迁不删）/ `danger`（会话、配置，锁定不清理）
     - `labelZh` / `labelEn`、`hintZh` / `hintEn`：这个目录是什么、删了会怎样
     - `paths`：精确路径，支持 `%ENV%` 与通配符
   - 红线：**会话记录、对话历史、文件历史、凭据文件一律 `danger` + `locked: true`**
3. 提 PR，附一张扫描截图（清理前后磁盘用量更佳）

## 目录结构

```
app.py            # 后端：HTTP 服务 + 扫描/清理/迁移引擎（Python 标准库，零依赖）
rules/*.json      # 清理规则（贡献主战场）
static/index.html # 前端（单文件，无构建；界面文案暂内嵌于其中的 I18N 对象，zh/en）
tests/            # 回归测试：test_rules.py 规则 lint；test_sandbox.py / test_http.py 沙盒与接口测试（不触碰真实目录）
```

## 本地开发

```bash
python app.py                # 网页界面 http://127.0.0.1:8520/
python app.py cli scan       # 命令行扫描
python tests/test_sandbox.py # 回归测试（沙盒）
```

## 规则原则

- 宁可少清，不可误删：不确定的目录标 `danger` 或干脆不收
- 每条 hint 要回答「删了会怎样」
- 路径用环境变量（`%APPDATA%` 等），不要写死用户名
