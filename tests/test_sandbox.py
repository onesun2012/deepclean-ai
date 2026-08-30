# -*- coding: utf-8 -*-
"""
深清 DeepClean 沙盒回归测试：不会触碰任何真实用户目录。

运行: python tests/test_sandbox.py
覆盖: 规则加载、locked/migrate 红线、沙盒扫描清理、min_age 保留期、
      目录排除、CLI 三态退出码、迁移（复制+junction+回滚语义）。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(APP_DIR, "app.py")
DAY = 86400
FAILED = []
TOTAL = 0


def check(name, cond):
    global TOTAL
    TOTAL += 1
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILED.append(name)


def run_cli(args, sandbox):
    env = dict(os.environ, CLEAR_C_SANDBOX=sandbox)
    p = subprocess.run([sys.executable, APP, "cli"] + args,
                       capture_output=True, text=True, encoding="utf-8", env=env, timeout=300)
    return p.returncode, p.stdout, p.stderr


def main():
    sandbox = tempfile.mkdtemp(prefix="deepclean_test_")
    try:
        # ---------- 1. 规则加载 ----------
        rc, out, _ = run_cli(["categories"], sandbox)
        data = json.loads(out)
        buckets = {b["id"]: b for b in data["buckets"]}
        check("cli categories 退出码为 0", rc == 0)
        check("分项数量 >= 50（规则文件加载正常）", len(buckets) >= 50)
        locked = [b for b in buckets.values() if b["locked"]]
        check("会话类分项全部 locked（永不清理）",
              all(b["risk"] == "danger" for b in locked) and
              {"claude-sessions", "codex-sessions", "cursor-history"} <= set(b["id"] for b in locked))
        check("migrate 分项已定义（HF/Ollama/LM Studio）",
              {"hf-hub", "ollama-models"} <= set(b["id"] for b in buckets.values() if b["risk"] == "migrate"))
        check("双语字段完整", all(b.get("name") and b.get("desc") for b in buckets.values()))

        # ---------- 2. 红线: locked 分项拒绝清理 ----------
        rc, out, _ = run_cli(["clean", "--ids", "claude-sessions", "--yes"], sandbox)
        check("locked 分项清理被拒绝（退出码 1）", rc == 1 and "锁定" in out)

        # ---------- 3. 沙盒扫描/清理 ----------
        sub = os.path.join(sandbox, "sub")
        os.makedirs(sub)
        old_file = os.path.join(sandbox, "old.txt")
        new_file = os.path.join(sub, "new.txt")
        open(old_file, "w").write("old junk")
        open(new_file, "w").write("fresh junk")
        old_t = time.time() - 30 * DAY
        os.utime(old_file, (old_t, old_t))

        sys.path.insert(0, APP_DIR)
        os.environ["CLEAR_C_SANDBOX"] = sandbox
        import app
        app.CAT_BY_ID["sandbox"]["min_age_min"] = 7 * 24 * 60
        app.SCAN.start()
        while app.SCAN.status in ("running", "stopping"):
            time.sleep(0.1)
        check("扫描完成", app.SCAN.status == "done")
        check("扫描统计到两个沙盒文件",
              app.SCAN.per["sandbox"]["size"] == len("old junk") + len("fresh junk"))
        plan_m, _ = app.build_clean_plan(["sandbox", "hf-hub", "ollama-models", "claude-sessions"])
        check("清理计划硬排除 migrate/locked 分项",
              plan_m is not None and set(plan_m.keys()) == {"sandbox"})
        plan, _ = app.build_clean_plan(["sandbox"])
        app.CLEAN.start(plan, False)
        while app.CLEAN.status in ("running", "stopping"):
            time.sleep(0.1)
        check("30 天前的旧文件已被删除", not os.path.exists(old_file))
        check("保留期内的新文件被跳过", os.path.exists(new_file))

        # ---------- 4. 按目录排除 ----------
        r1 = os.path.join(sandbox, "keep_me")
        r2 = os.path.join(sandbox, "clean_me")
        os.makedirs(r1); os.makedirs(r2)
        f1 = os.path.join(r1, "a.txt"); f2 = os.path.join(r2, "b.txt")
        open(f1, "w").write("keep"); open(f2, "w").write("clean")
        app.CAT_BY_ID["sandbox"]["min_age_min"] = 0
        app.CAT_BY_ID["sandbox"]["roots"] = [r1, r2]
        app.SCAN._reset()
        app.SCAN.start()
        while app.SCAN.status in ("running", "stopping"):
            time.sleep(0.1)
        check("roots 明细按目录统计", len(app.SCAN.roots_info["sandbox"]) == 2)
        plan, _ = app.build_clean_plan(["sandbox"], excluded=[r1])
        check("排除目录后计划只含另一目录的文件",
              len(plan["sandbox"]["entries"]) == 1 and plan["sandbox"]["entries"][0][0] == f2)
        app.CLEAN.start(plan, False)
        while app.CLEAN.status in ("running", "stopping"):
            time.sleep(0.1)
        check("被排除目录的文件未被删除", os.path.exists(f1))
        check("未排除目录的文件已被删除", not os.path.exists(f2))

        # ---------- 5. CLI 退出码语义 ----------
        open(new_file, "w").write("again")
        rc, out, _ = run_cli(["clean", "--ids", "sandbox"], sandbox)
        check("无 --yes 退出码 2（待确认）", rc == 2 and "need_confirmation" in out)
        check("无 --yes 未删除文件", os.path.exists(new_file))
        rc, out, _ = run_cli(["clean", "--ids", "sandbox", "--yes"], sandbox)
        check("cli clean --yes 退出码 0", rc == 0)
        check("沙盒文件已实际删除", not os.path.exists(new_file))
        rc, out, _ = run_cli(["clean", "--ids", "not_a_bucket", "--yes"], sandbox)
        check("未知分项退出码 1", rc == 1)

        # ---------- 6. 迁移: 复制 + junction + 失败回滚语义 ----------
        src_dir = os.path.join(sandbox, "mymodels")
        os.makedirs(os.path.join(src_dir, "weights"))
        open(os.path.join(src_dir, "weights", "m.gguf"), "w").write("x" * 1024)
        app.CATEGORIES.append(dict(id="move-test", tool="movetest", category="model", risk="migrate",
                                   name="迁移测试", nameEn="Move test", desc="", descEn="",
                                   moveable=True, move_root=src_dir, roots=[]))
        job = app.MoveJob()
        rc_ok, err = job.start("movetest", sandbox[:2], False)  # 沙盒所在盘
        # start 用 move_root 找 src；目标盘取沙盒盘符 —— dst 在沙盒下不成立(同盘复制允许)
        while job.status == "running":
            time.sleep(0.1)
        snap = job.snapshot()
        junction = src_dir
        dst = snap["info"].get("dst", "")
        check("迁移任务完成", rc_ok and snap["status"] == "done")
        check("目标目录包含模型文件", os.path.isfile(os.path.join(dst, "weights", "m.gguf")))
        check("原路径已变为目录联接且可访问",
              os.path.realpath(junction).lower() == os.path.realpath(dst).lower()
              and os.path.isfile(os.path.join(junction, "weights", "m.gguf")))
        # 再次迁移应因目标已存在而失败且源不受影响
        job2 = app.MoveJob()
        job2.start("movetest", sandbox[:2], False)
        while job2.status == "running":
            time.sleep(0.1)
        snap2 = job2.snapshot()
        check("重复迁移被拒绝（目标已存在）", snap2["status"] == "error" and "已存在" in snap2["error"])
        check("失败后原联接仍然可用", os.path.isfile(os.path.join(junction, "weights", "m.gguf")))
        # 清理 junction（rmdir 只删联接不删目标），确认目标真实数据仍完整后再清理
        os.rmdir(junction)
        check("junction 删除后目标真实数据仍完整", os.path.isfile(os.path.join(dst, "weights", "m.gguf")))
        shutil.rmtree(dst, ignore_errors=True)
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)

    print("\n%d 项通过, %d 项失败" % (TOTAL - len(FAILED), len(FAILED)))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
