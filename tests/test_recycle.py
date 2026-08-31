# -*- coding: utf-8 -*-
"""
深清 DeepClean 回收站删除语义测试（全程 mock，不碰真实回收站）。

运行: python tests/test_recycle.py
覆盖: Windows 生产路径 SHFileOperationW(FOF_ALLOWUNDO)、失败跳过且不 os.remove、
      沙盒路径走 os.remove、recycle-bin 单独执行拦截、history.jsonl 写入（dry 不写）、
      GET /api/history 与 Host 校验。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_DIR)
FAILED = []
TOTAL = 0

FO_DELETE, FOF_ALLOWUNDO = 3, 0x40


def check(name, cond):
    global TOTAL
    TOTAL += 1
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILED.append(name)


def wait_clean():
    while app.CLEAN.status in ("running", "stopping"):
        time.sleep(0.05)


def scan_sandbox():
    app.SCAN.start()
    while app.SCAN.status in ("running", "stopping"):
        time.sleep(0.05)


def main():
    global app
    sandbox = tempfile.mkdtemp(prefix="deepclean_recycle_")
    hist_dir = tempfile.mkdtemp(prefix="deepclean_hist_")  # 历史放沙盒外，模拟 LOCALAPPDATA\DeepClean
    hist = os.path.join(hist_dir, "history.jsonl")
    os.environ["CLEAR_C_SANDBOX"] = sandbox
    os.environ["DEEPCLEAN_HISTORY"] = hist
    import app

    app.SCAN.cats = [app.CAT_BY_ID["sandbox"]]
    scan_sandbox()

    def run_clean(ids, dry=False):
        plan, err = app.build_clean_plan(ids)
        assert plan is not None, err
        app.CLEAN.start(plan, dry)
        wait_clean()

    try:
        # ---- a. 生产路径（unset 沙盒 env）：SHFileOperation + FOF_ALLOWUNDO，不 os.remove ----
        f_a = os.path.join(sandbox, "a.txt")
        open(f_a, "w").write("a" * 100)
        scan_sandbox()
        calls = []

        def fake_ok(op_ptr):
            op = op_ptr._obj
            calls.append((op.fFlags, op.wFunc))
            return 0  # mock：Shell 成功

        app._sh_recycle = fake_ok
        os.environ.pop("CLEAR_C_SANDBOX", None)
        run_clean(["sandbox"])
        check("生产路径调用了 SHFileOperation", len(calls) > 0)
        check("fFlags 含 FOF_ALLOWUNDO 且 wFunc=FO_DELETE",
              all(f & FOF_ALLOWUNDO and w == FO_DELETE for f, w in calls))
        check("mock 成功但未 os.remove（文件仍在磁盘）", os.path.exists(f_a))
        snap = app.CLEAN.snapshot()
        check("via=recycle 且 recycled=1",
              snap.get("via") == "recycle" and snap["per"]["sandbox"].get("recycled") == 1)

        # ---- e1. 非 dry 清理写历史（含 ids/total_freed/via）----
        check("history.jsonl 已写入且含 ids/total_freed/via",
              any(r.get("via") == "recycle" and "sandbox" in r.get("ids", [])
                  and "total_freed" in r for r in app.read_history(50)))

        # ---- e2. dry 清理不写历史 ----
        lines_before = len(open(hist, encoding="utf-8").read().splitlines())
        run_clean(["sandbox"], dry=True)
        lines_after = len(open(hist, encoding="utf-8").read().splitlines())
        check("dry 清理不写历史", lines_after == lines_before)

        # ---- d. recycle-bin 不能与其它分项同趟 ----
        plan, err = app.build_clean_plan(["sandbox", "recycle-bin"])
        check("recycle-bin 混勾被拒绝", plan is None and "单独执行" in err)
        plan2, _ = app.build_clean_plan(["recycle-bin"])
        check("recycle-bin 单独可进计划", plan2 is not None)

        # ---- b. mock Shell 失败：计入 skipped、不 os.remove ----
        f_b = os.path.join(sandbox, "b.txt")
        open(f_b, "w").write("b" * 50)
        scan_sandbox()

        def fake_fail(op_ptr):
            return 1  # mock：Shell 失败

        app._sh_recycle = fake_fail
        run_clean(["sandbox"])
        check("SH 失败后文件仍在（未 os.remove）", os.path.exists(f_b))
        snap = app.CLEAN.snapshot()
        check("失败计入 skipped 且 recycled=0",
              snap["per"]["sandbox"]["recycled"] == 0
              and snap["per"]["sandbox"]["skipped"] >= 1)

        # ---- c. 沙盒路径走 os.remove，不调用 SHFileOperation ----
        f_c = os.path.join(sandbox, "c.txt")
        open(f_c, "w").write("c" * 30)
        scan_sandbox()
        calls_n = len(calls)
        os.environ["CLEAR_C_SANDBOX"] = sandbox
        run_clean(["sandbox"])
        check("沙盒路径走 os.remove（文件已消失）", not os.path.exists(f_c))
        check("沙盒路径未调用 SHFileOperation", len(calls) == calls_n)

        # ---- f. GET /api/history（Host 校验不回归）----
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % port

        def http(method, path, headers=None):
            r = urllib.request.Request(base + path, method=method)
            for k, v in (headers or {}).items():
                r.add_header(k, v)
            try:
                with urllib.request.urlopen(r, timeout=10) as resp:
                    return resp.status, resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode("utf-8", "replace")

        try:
            code, out = http("GET", "/api/history?limit=20")
            data = json.loads(out)
            check("GET /api/history 200 且含本次记录",
                  code == 200 and data.get("ok") is True
                  and any(r.get("via") == "recycle" for r in data.get("items", [])))
            code, _ = http("GET", "/api/history", headers={"Host": "evil.com"})
            check("伪造 Host 访问 history 仍 403", code == 403)
        finally:
            httpd.shutdown()
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
        shutil.rmtree(hist_dir, ignore_errors=True)

    print("\n%d 项通过, %d 项失败" % (TOTAL - len(FAILED), len(FAILED)))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
