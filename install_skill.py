# -*- coding: utf-8 -*-
"""
把本工具封装的 skill 一键安装到各 AI 编程工具的技能目录。

- 安装: python install_skill.py
- 卸载: python install_skill.py --uninstall

SKILL.md 中 {{APP_DIR}} 占位符会被替换为工具本体的绝对路径，
各 AI 工具（Claude Code / Codex / 及识别 ~/.agents/skills 的工具）加载该技能后，
即可按 SKILL.md 的指引通过 `python app.py cli ...` 完成扫描与清理。
纯 Python 标准库实现，无任何第三方依赖。
"""
import os
import shutil
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(APP_DIR, "skill", "SKILL.md")
SKILL_NAME = "clear-c"

# 通用共享位置 + 各工具用户级技能目录（不存在时会自动创建）
TARGETS = [
    ("跨工具共享 ~/.agents/skills", os.path.join(os.path.expanduser("~"), ".agents", "skills")),
    ("Claude Code ~/.claude/skills", os.path.join(os.path.expanduser("~"), ".claude", "skills")),
    ("Codex ~/.codex/skills", os.path.join(os.path.expanduser("~"), ".codex", "skills")),
]


def install():
    if not os.path.exists(SRC):
        print("找不到 %s，请勿移动 install_skill.py 的位置" % SRC)
        return 1
    with open(SRC, encoding="utf-8") as f:
        body = f.read().replace("{{APP_DIR}}", APP_DIR)
    ok = 0
    for label, base in TARGETS:
        dest_dir = os.path.join(base, SKILL_NAME)
        dest = os.path.join(dest_dir, "SKILL.md")
        try:
            os.makedirs(dest_dir, exist_ok=True)
            with open(dest, "w", encoding="utf-8") as f:
                f.write(body)
            print("已安装  %-24s -> %s" % (label, dest))
            ok += 1
        except OSError as e:
            print("安装失败  %-22s %s" % (label, e))
    print("\n完成：%d/%d 处。技能指向工具本体 %s" % (ok, len(TARGETS), APP_DIR))
    print("在 Claude Code / Codex 等工具里直接说\"帮我清理C盘\"即可触发；ZCode 等支持 .agents 技能目录的工具同样可用。")
    return 0 if ok else 1


def uninstall():
    ok = 0
    for label, base in TARGETS:
        dest_dir = os.path.join(base, SKILL_NAME)
        if os.path.isdir(dest_dir):
            try:
                shutil.rmtree(dest_dir)
                print("已卸载  %s (%s)" % (dest_dir, label))
                ok += 1
            except OSError as e:
                print("卸载失败  %s: %s" % (dest_dir, e))
        else:
            print("未安装  %s" % dest_dir)
    return 0


if __name__ == "__main__":
    if "--uninstall" in sys.argv[1:]:
        sys.exit(uninstall())
    sys.exit(install())
