# -*- coding: utf-8 -*-
"""
深清 DeepClean 规则 lint：不启动服务、不触碰任何目录，只审计 rules/*.json。

运行: python tests/test_rules.py
覆盖: 结构完整性（id 唯一 / tool 引用 / 双语字段）、安全级约束（danger⇔locked、
      migrate+moveable⇒move_root）、红线（会话与回滚数据路径不得出现在
      safe/rebuildable 分项）、本批锁定项回归。
"""
import json
import os
import re
import sys

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULES_DIR = os.path.join(APP_DIR, "rules")
FAILED = []
TOTAL = 0


def check(name, cond):
    global TOTAL
    TOTAL += 1
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILED.append(name)


def norm(p):
    return p.replace("/", "\\").strip().lower()


def main():
    files = sorted(f for f in os.listdir(RULES_DIR) if f.endswith(".json"))
    check("rules 目录存在 json 规则文件", len(files) >= 5)

    raw_texts = {}
    for fn in files:
        with open(os.path.join(RULES_DIR, fn), encoding="utf-8") as f:
            raw_texts[fn] = f.read()
    data = {fn: json.loads(t) for fn, t in raw_texts.items()}

    tools, buckets = {}, []
    for fn, d in data.items():
        for t in d.get("tools", []):
            tools[t["id"]] = t
        for b in d.get("buckets", []):
            buckets.append((fn, b))

    ids = [b["id"] for _, b in buckets]
    check("分项 id 全局唯一", len(ids) == len(set(ids)))
    check("每个分项的 tool 引用存在", all(b.get("tool") in tools for _, b in buckets))
    check("risk 枚举合法",
          all(b.get("risk") in ("safe", "rebuildable", "migrate", "danger") for _, b in buckets))
    check("danger 分项全部 locked", all(b.get("locked") for _, b in buckets if b.get("risk") == "danger"))
    check("locked 分项全部 danger", all(b.get("risk") == "danger" for _, b in buckets if b.get("locked")))
    check("双语字段完整",
          all(b.get("labelZh") and b.get("labelEn") and b.get("hintZh") and b.get("hintEn")
              for _, b in buckets))
    check("非特殊分项必须声明 paths",
          all(b.get("paths") for _, b in buckets
              if not (b.get("special_size") or b.get("special_clean"))))
    check("migrate+moveable 必须声明 move_root",
          all(b.get("move_root") for _, b in buckets
              if b.get("risk") == "migrate" and b.get("moveable")))
    check("moveable 分项必须同时是 migrate",
          all(b.get("risk") == "migrate" for _, b in buckets if b.get("moveable")))
    bad_user = ["%s: %s" % (b["id"], p)
                for _, b in buckets for p in b.get("paths", [])
                if re.search(r"c:\\users\\[^%]", norm(p))]
    check("paths 不含写死的用户目录字面量（须用 %USERPROFILE% 等）", not bad_user)
    for x in bad_user:
        print("      违规: " + x)
    bad_admin = [b["id"] for _, b in buckets
                 if b.get("special_clean") and b["special_clean"] != "recycle"
                 and not b.get("clean_admin")]
    check("有 special_clean 的分项必须声明 clean_admin（recycle 除外）", not bad_admin)

    # ---- 红线：会话/文件历史/回滚数据路径不得出现在 safe / rebuildable 分项 ----
    REDLINE_MARKERS = ("rollout", "file-history", "user\\history", "\\sessions",
                       "\\projects", "windows.old", "globalstorage", ".zcode\\cli\\db")
    bad = []
    for _, b in buckets:
        if b.get("risk") in ("safe", "rebuildable"):
            for p in b.get("paths", []):
                low = norm(p)
                if any(m in low for m in REDLINE_MARKERS):
                    bad.append("%s: %s" % (b["id"], p))
    check("safe/rebuildable 分项不含会话与回滚数据路径", not bad)
    for x in bad:
        print("      违规: " + x)

    # ---- 同一路径不得同时登记在可清理与其它分项（防规则改错留副本）----
    cleanable_paths = {}
    for _, b in buckets:
        if b.get("risk") in ("safe", "rebuildable"):
            for p in b.get("paths", []):
                cleanable_paths.setdefault(norm(p), b["id"])
    dup = []
    for _, b in buckets:
        if b.get("risk") in ("safe", "rebuildable"):
            continue
        for p in b.get("paths", []):
            if norm(p) in cleanable_paths:
                dup.append(norm(p))
    check("路径不与其它分项重复登记", not dup)

    # ---- Windows.old 必须绝迹 ----
    check("Windows.old 不出现在任何规则文件",
          not any("windows.old" in t.lower() for t in raw_texts.values()))

    # ---- 本批回归：必须保持 locked 的分项 ----
    by_id = {b["id"]: b for _, b in buckets}
    need_locked = {"claude-sessions", "codex-sessions", "cursor-history",
                   "zcode-sessions", "cursor-file-history",
                   "winsxs", "pagefile", "hibernate", "restore-points"}
    check("会话/文件历史与系统危险项全部锁定",
          need_locked <= {i for i, b in by_id.items() if b.get("locked")})
    rollout_owner = [i for i, b in by_id.items()
                     for p in b.get("paths", []) if "rollout" in norm(p)]
    check("zcode rollout 只登记在 locked 分项",
          bool(rollout_owner) and all(by_id[i].get("locked") for i in rollout_owner))
    check("recycle-bin 为 rebuildable（安全模式不再默认勾选）",
          by_id.get("recycle-bin", {}).get("risk") == "rebuildable")
    check("recycle-bin 三种模式均默认不勾选（default_off）",
          by_id.get("recycle-bin", {}).get("default_off") is True)

    print("\n%d 项通过, %d 项失败" % (TOTAL - len(FAILED), len(FAILED)))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
