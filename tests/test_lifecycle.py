"""Real process lifecycle; isolated cache/runtime, never scans production rules."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from instance import Instance

executable = os.environ.get("DEEPCLEAN_TEST_EXE")
command = [executable] if executable else [sys.executable, str(PROJECT / "app.py")]
installation = str(Path(executable).resolve().parent) if executable else str(PROJECT)
with tempfile.TemporaryDirectory(prefix="deepclean_lifecycle_") as root:
    cache = Path(root) / "cache"
    cache.mkdir()
    (cache / "untouched.txt").write_text("keep")
    runtime = str(Path(root) / "runtime")
    os.environ["DEEPCLEAN_RUNTIME_DIR"] = runtime
    env = dict(os.environ, CLEAR_C_SANDBOX=str(cache), CLEAR_C_NO_BROWSER="1", PYTHONIOENCODING="utf-8",
               DEEPCLEAN_HISTORY=str(Path(root) / "history.jsonl"))
    import ctypes
    lease = Instance(installation, bool(ctypes.windll.shell32.IsUserAnAdmin()))
    reserved = socket.socket()
    try:
        reserved.bind(("127.0.0.1", 8520))
        reserved.listen()
    except OSError:
        reserved.close()
    process = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 45
        while True:
            assert process.poll() is None, "server exited before ready"
            try:
                state = lease.session()
                url = "http://127.0.0.1:%d" % state["port"]
                def request(path, body=None, auth=True):
                    req = urllib.request.Request(url + path, data=body,
                        headers={"X-DeepClean-Token":state["token"]} if auth else {})
                    with urllib.request.urlopen(req, timeout=3) as response:
                        return response.read()
                assert json.loads(request("/api/progress"))["status"] == "idle"
                break
            except (OSError, ValueError):
                if time.monotonic() > deadline:
                    raise AssertionError("server readiness timeout")
                time.sleep(.1)
        assert state["port"] != 8520
        assert state["token"].encode() not in (lease.directory / "session.bin").read_bytes()
        second = subprocess.run(command, env=env, timeout=30, capture_output=True)
        assert second.returncode == 0, "duplicate launch failed"
        assert process.poll() is None
        assert lease.session() == state, "duplicate created a second session"
        data = json.loads(request("/api/state"))
        assert len(data["support_links"]) == 5
        for name in ("donate-wechat.png", "donate-alipay.png"):
            assert request("/support/" + name) == (PROJECT / "static" / "support" / name).read_bytes()
        assert json.loads(request("/api/scan/start", b'{"ids":["sandbox"]}'))["ok"]
        while json.loads(request("/api/progress"))["status"] in ("running", "stopping"):
            assert time.monotonic() < deadline
            time.sleep(.05)
        assert json.loads(request("/api/progress"))["scope"] == ["sandbox"]
        stop = subprocess.run(command + ["--stop"], env=env, timeout=30, capture_output=True)
        assert stop.returncode == 0
        assert process.wait(timeout=30) == 0
        assert not (lease.directory / "session.bin").exists()
        assert (cache / "untouched.txt").read_text() == "keep"
        print("Lifecycle passed: port fallback, DPAPI session, duplicate launch, assets, scoped scan, authenticated stop, data retained")
    finally:
        reserved.close()
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=15)
