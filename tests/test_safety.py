"""Isolated filesystem + HTTP regression tests; never scan real rules."""
import ctypes
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
import urllib.request
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ROOT = Path(tempfile.mkdtemp(prefix="deepclean_safety_"))
SANDBOX = ROOT / "cache"
SANDBOX.mkdir()
os.environ["CLEAR_C_SANDBOX"] = str(SANDBOX)
os.environ["DEEPCLEAN_HISTORY"] = str(ROOT / "history" / "history.jsonl")
import app
from safety import plain_path


def wait(job):
    deadline = time.monotonic() + 10
    while job.status in ("running", "stopping"):
        if time.monotonic() > deadline:
            raise AssertionError("job did not terminate")
        time.sleep(.01)
    # Terminal publication happens before the worker's final lock release.
    with app.OPERATION_LOCK:
        pass


class SafetyTests(unittest.TestCase):
    def test_exclusive_listener_does_not_share_ports(self):
        with ThreadingHTTPServer(("127.0.0.1", 0), app.Handler) as occupied:
            with self.assertRaises(OSError):
                app.LocalHTTPServer(occupied.server_address, app.Handler)
        with app.LocalHTTPServer(("127.0.0.1", 0), app.Handler) as exclusive:
            with socket.socket() as contender:
                contender.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                with self.assertRaises(OSError):
                    contender.bind(exclusive.server_address)

    def setUp(self):
        self.admin = patch.object(app, "is_admin", return_value=False)
        self.admin.start()
        self.proc = patch.object(app, "running_processes", return_value=set())
        self.proc.start()
        self.cat = app.CAT_BY_ID["sandbox"]
        self.cat["roots"] = [str(SANDBOX)]
        self.cat["min_age_min"] = 0
        self.cat.pop("locked", None)
        self.cat["risk"] = "safe"
        app.SCAN = app.ScanJob([self.cat])
        app.CLEAN = app.CleanJob()
        self.file = SANDBOX / "old.txt"
        self.file.write_text("old")

    def tearDown(self):
        self.admin.stop()
        self.proc.stop()
        for path in SANDBOX.iterdir():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()

    def scan(self):
        self.assertTrue(app.SCAN.start()[0])
        wait(app.SCAN)
        self.assertEqual(app.SCAN.status, "done")
        plan, error = app.build_clean_plan(["sandbox"])
        self.assertIsNotNone(plan, error)
        return plan

    def clean(self, plan):
        self.assertTrue(app.CLEAN.start(plan, False)[0])
        wait(app.CLEAN)
        self.assertEqual(app.CLEAN.status, "done")

    def test_normal_control_and_stale_results(self):
        plan = self.scan()
        self.clean(plan)
        self.assertFalse(self.file.exists())
        self.assertEqual(app.SCAN.status, "stale")
        self.assertIsNone(app.build_clean_plan(["sandbox"])[0])
        self.assertFalse(app.CLEAN.start(plan, False)[0])

    def test_changed_file_is_retained(self):
        plan = self.scan()
        self.file.write_text("new important data")
        self.clean(plan)
        self.assertEqual(self.file.read_text(), "new important data")

    def test_replaced_file_is_retained(self):
        plan = self.scan()
        self.file.rename(SANDBOX / "original.txt")
        self.file.write_text("new")
        self.clean(plan)
        self.assertTrue(self.file.exists())

    def test_age_limit(self):
        self.cat["min_age_min"] = 60
        self.clean(self.scan())
        self.assertTrue(self.file.exists())

    def test_excluded_subtree_and_empty_directory(self):
        sub = SANDBOX / "sub"
        sub.mkdir()
        (sub / "keep.txt").write_text("keep")
        (sub / "empty").mkdir()
        self.scan()
        plan, _ = app.build_clean_plan(["sandbox"], [str(sub).upper() if app.IS_WIN else str(sub)])
        self.clean(plan)
        self.assertTrue((sub / "keep.txt").exists())
        self.assertTrue((sub / "empty").exists())
        self.assertFalse(self.file.exists())

    def test_admin_mutations_rejected_at_shared_boundaries(self):
        plan = self.scan()
        with patch.object(app, "is_admin", return_value=True), patch.object(app, "_sh_recycle") as shell:
            self.assertFalse(app.CLEAN.start(plan, False)[0])
            self.assertFalse(app.delete_files([str(self.file)])[0])
            self.assertFalse(app.empty_recycle_bin()[0])
            self.assertFalse(app.MOVE.start("ollama", "D", False)[0])
            shell.assert_not_called()
            self.assertTrue(app.CLEAN.start(plan, True)[0])
            wait(app.CLEAN)
        self.assertTrue(self.file.exists())

    def test_permanent_delete_cannot_escape_sandbox(self):
        outside = ROOT / "outside.txt"
        outside.write_text("keep")
        self.assertEqual(app.delete_files([str(outside)])[0], [])
        self.assertTrue(outside.exists())

    def test_expired_and_rescanned_plan_rejected(self):
        plan = self.scan()
        plan["sandbox"]["expires"] = 0
        self.assertFalse(app.CLEAN.start(plan, False)[0])
        old = self.scan()
        self.scan()
        self.assertFalse(app.CLEAN.start(old, False)[0])

    def test_scan_clean_mutex_and_stopping(self):
        plan = self.scan()
        entered, release = threading.Event(), threading.Event()
        original = app.CLEAN._clean_generic
        def blocked(*args):
            entered.set()
            release.wait(5)
            return original(*args)
        with patch.object(app.CLEAN, "_clean_generic", side_effect=blocked):
            self.assertTrue(app.CLEAN.start(plan, False)[0])
            self.assertTrue(entered.wait(5))
            try:
                app.CLEAN.stop_clean()
                self.assertFalse(app.CLEAN.start(plan, False)[0])
                self.assertFalse(app.SCAN.start()[0])
                self.assertIsNone(app.build_clean_plan(["sandbox"])[0])
            finally:
                release.set()
                wait(app.CLEAN)
        self.assertTrue(self.file.exists())

    def test_worker_exception_releases_operation_lock(self):
        with patch.object(app.SCAN, "_scan_cat", side_effect=RuntimeError("fixture")):
            self.assertTrue(app.SCAN.start()[0])
            wait(app.SCAN)
            self.assertEqual(app.SCAN.status, "error")
        self.scan()

    def test_locked_bucket_has_statistics_but_no_file_list(self):
        self.cat["locked"] = True
        self.assertTrue(app.SCAN.start()[0])
        wait(app.SCAN)
        self.assertGreater(app.SCAN.per["sandbox"]["size"], 0)
        self.assertEqual(app.SCAN.entries["sandbox"], [])
        self.assertEqual(app.SCAN.identities, {})

    @unittest.skipUnless(sys.platform == "win32", "Windows junction fixture")
    def test_root_ancestor_and_postscan_junction(self):
        target = ROOT / "junction-target"
        target.mkdir(exist_ok=True)
        (target / "old.txt").write_text("outside")
        link = SANDBOX / "link"
        def junction():
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        junction()
        try:
            self.assertFalse(plain_path(str(link)))
            self.assertFalse(plain_path(str(link / "old.txt")))
            self.scan()
            self.assertEqual(len(app.SCAN.entries["sandbox"]), 1)
        finally:
            os.rmdir(link)
        link.mkdir()
        (link / "old.txt").write_text("original")
        plan = self.scan()
        link.rename(SANDBOX / "saved")
        junction()
        try:
            self.clean(plan)
            self.assertEqual((target / "old.txt").read_text(), "outside")
        finally:
            os.rmdir(link)

    def test_abi(self):
        self.assertEqual(app.SHQUERYRBINFO.i64Size.offset, 8)
        self.assertEqual(app.SHQUERYRBINFO.i64NumItems.offset, 16)
        self.assertEqual(ctypes.sizeof(app.SHQUERYRBINFO), 24)

    def test_scope_does_not_visit_other_categories(self):
        other = dict(self.cat, id="other")
        app.SCAN.cats = [self.cat, other]
        with patch.object(app.SCAN, "_scan_cat", wraps=app.SCAN._scan_cat) as scan:
            self.assertTrue(app.SCAN.start(["sandbox"])[0])
            wait(app.SCAN)
            self.assertEqual([call.args[0]["id"] for call in scan.call_args_list], ["sandbox"])
        self.assertEqual(app.SCAN.per["other"]["status"], "not_scanned")
        self.assertEqual(app.SCAN.snapshot()["percent"], 100)

    def test_global_budget_disables_cleanup(self):
        (SANDBOX / "second.txt").write_text("second")
        with patch.object(app, "MAX_SCAN_ENTRIES", 1):
            self.assertTrue(app.SCAN.start()[0])
            wait(app.SCAN)
        self.assertTrue(app.SCAN.snapshot()["partial"])
        self.assertEqual(len(app.SCAN.entries["sandbox"]), 1)
        self.assertIsNone(app.build_clean_plan(["sandbox"])[0])
        self.assertTrue(self.file.exists())

    def test_http_auth_input_and_approval(self):
        self.scan()
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_port
        def request(path, body=None, token=app.API_TOKEN):
            headers = {"Content-Type":"application/json"}
            if token is not None:
                headers["X-DeepClean-Token"] = token
            req = urllib.request.Request(base + path, data=body, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=5) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as error:
                return error.code, error.read()
        try:
            app.OPERATION_LOCK.acquire()
            try:
                self.assertFalse(json.loads(request("/api/shutdown", b"{}")[1])["ok"])
            finally:
                app.OPERATION_LOCK.release()
            for path in ("state", "history", "roots", "progress", "clean/progress", "move/progress"):
                self.assertEqual(request("/api/" + path)[0], 200)
                self.assertEqual(request("/api/" + path, token=None)[0], 401)
                self.assertEqual(request("/api/" + path, token="wrong")[0], 401)
            self.assertEqual(request("/api/scan/start", b"{}", None)[0], 401)
            for body in (b"[]", b"null", b"{", b'{"dry":"false"}', b'{"ids":"sandbox"}'):
                self.assertEqual(request("/api/clean", body)[0], 400)
            self.assertNotIn(app.API_TOKEN.encode(), request("/", token=None)[1])
            self.assertFalse(json.loads(request("/api/clean", b'{"ids":["sandbox"]}')[1])["ok"])
            preview = json.loads(request("/api/clean/preview", b'{"ids":["sandbox"]}')[1])
            body = json.dumps({"plan_token":preview["plan_token"]}).encode()
            self.assertTrue(json.loads(request("/api/clean", body)[1])["ok"])
            wait(app.CLEAN)
            self.assertFalse(json.loads(request("/api/clean", body)[1])["ok"])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(ROOT)
