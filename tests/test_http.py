# -*- coding: utf-8 -*-
"""
深清 DeepClean HTTP 接口安全测试（纯标准库，全部为 dry/预览请求，不实际删除）。

运行: python tests/test_http.py
覆盖: Host / Origin / Sec-Fetch-Site 来源校验、超大请求体 413、
      危险分项未确认不进清理计划、locked 不可被 confirm_danger 覆盖。
"""
import json
import os
import shutil
import socket
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


def check(name, cond):
    global TOTAL
    TOTAL += 1
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILED.append(name)


def main():
    sandbox = tempfile.mkdtemp(prefix="deepclean_http_")
    junk = os.path.join(sandbox, "junk.txt")
    open(junk, "w").write("junk")
    os.environ["CLEAR_C_SANDBOX"] = sandbox
    import app

    # 只扫沙盒分项：更快，也不读真实用户目录
    app.SCAN.cats = [app.CAT_BY_ID["sandbox"]]
    app.SCAN.start()
    while app.SCAN.status in ("running", "stopping"):
        time.sleep(0.05)
    check("扫描完成且统计到沙盒文件", app.SCAN.status == "done" and app.SCAN.per["sandbox"]["size"] > 0)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % port

    def http(method, path, body=None, headers=None):
        r = urllib.request.Request(base + path, data=body, method=method)
        if body is not None:
            r.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            r.add_header(k, v)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    try:
        # ---- Host 校验（防 DNS rebinding）----
        code, _ = http("GET", "/api/state")
        check("本机 Host 的 GET /api/state 返回 200", code == 200)
        code, _ = http("GET", "/api/state", headers={"Host": "evil.com"})
        check("伪造 Host 返回 403", code == 403)
        code, _ = http("GET", "/api/state", headers={"Host": "localhost:%d" % port})
        check("localhost Host 视为本机（200）", code == 200)

        # ---- POST 来源校验（防其它网页驱动本接口）----
        code, _ = http("POST", "/api/clean", body=json.dumps({"ids": [], "dry": True}).encode(),
                       headers={"Origin": "http://evil.com"})
        check("跨站 Origin 的 POST 返回 403", code == 403)
        check("被拒请求未产生清理任务", app.CLEAN.status == "idle")
        code, _ = http("POST", "/api/clean", body=json.dumps({"ids": [], "dry": True}).encode(),
                       headers={"Sec-Fetch-Site": "cross-site"})
        check("Sec-Fetch-Site: cross-site 返回 403", code == 403)
        code, out = http("POST", "/api/clean",
                         body=json.dumps({"ids": ["sandbox"], "dry": True}).encode(),
                         headers={"Origin": base})
        data = json.loads(out)
        check("同源 Origin + dry 清理放行（200 且计划成立）",
              code == 200 and data.get("ok") is True and os.path.exists(junk))
        while app.CLEAN.status == "running":
            time.sleep(0.05)
        code, _ = http("POST", "/api/clean", body=json.dumps({"ids": [], "dry": True}).encode())
        check("无 Origin 的本地客户端（CLI 风格）放行", code == 200)

        # ---- 超大请求体 413（假 Content-Length，不真发 1MB 数据）----
        s = socket.create_connection(("127.0.0.1", port), timeout=10)
        s.sendall(("POST /api/clean HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
                   "Content-Type: application/json\r\nContent-Length: 1000001\r\n"
                   "Connection: close\r\n\r\n{}" % port).encode())
        resp = s.recv(4096)
        s.close()
        check("超大 Content-Length 返回 413", b" 413 " in resp.split(b"\r\n", 1)[0])

        # ---- 危险分项确认门（sandbox 临时置为未锁定的 danger）----
        app.CAT_BY_ID["sandbox"]["risk"] = "danger"
        code, out = http("POST", "/api/clean",
                         body=json.dumps({"ids": ["sandbox"], "dry": True}).encode())
        data = json.loads(out)
        check("未确认的危险分项不进清理计划", code == 200 and data.get("ok") is False)
        check("危险分项未确认时文件未被删除", os.path.exists(junk))
        code, out = http("POST", "/api/clean",
                         body=json.dumps({"ids": ["sandbox"], "dry": True,
                                          "confirm_danger": True}).encode())
        data = json.loads(out)
        check("confirm_danger 后计划放行（dry 不删除）",
              code == 200 and data.get("ok") is True and os.path.exists(junk))
        while app.CLEAN.status == "running":
            time.sleep(0.05)
        app.CAT_BY_ID["sandbox"]["risk"] = "safe"

        # ---- locked 不可被 confirm_danger 覆盖 ----
        code, out = http("POST", "/api/clean",
                         body=json.dumps({"ids": ["claude-sessions"], "dry": True,
                                          "confirm_danger": True}).encode())
        data = json.loads(out)
        check("locked 分项即使 confirm_danger 也被拒绝", code == 200 and data.get("ok") is False)
    finally:
        httpd.shutdown()
        shutil.rmtree(sandbox, ignore_errors=True)

    print("\n%d 项通过, %d 项失败" % (TOTAL - len(FAILED), len(FAILED)))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
