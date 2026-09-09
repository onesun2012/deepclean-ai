"""Real handle deletion tests, restricted to this module's temporary directory."""
import os
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ROOT = Path(tempfile.mkdtemp(prefix="deepclean_direct_"))
os.environ["CLEAR_C_SANDBOX"] = str(ROOT / "cache")
os.environ["DEEPCLEAN_HISTORY"] = str(ROOT / "history" / "events.jsonl")
import app
from safety import fingerprint, delete_verified_file


class PermanentTests(unittest.TestCase):
    def setUp(self):
        self.cache = ROOT / "cache"
        self.cache.mkdir(exist_ok=True)
        self.path = self.cache / "old.tmp"
        self.path.write_text("disposable fixture")
        old = time.time() - 9 * 86400
        os.utime(self.path, (old, old))
        self.expected = fingerprint(str(self.path))
        self.admin = patch.object(app, "is_admin", return_value=False)
        self.admin.start()
        self.cat = app.CAT_BY_ID["sandbox"]

    def tearDown(self):
        self.admin.stop()
        shutil.rmtree(self.cache)

    def test_delete_same_verified_handle(self):
        self.assertTrue(delete_verified_file(str(self.path), self.expected))
        self.assertFalse(self.path.exists())

    def test_busy_file_is_retained(self):
        with self.path.open("rb"):
            self.assertFalse(delete_verified_file(str(self.path), self.expected))
        self.assertTrue(self.path.exists())

    def test_replacement_is_retained(self):
        self.path.rename(self.cache / "original.tmp")
        self.path.write_text("replacement")
        self.assertFalse(delete_verified_file(str(self.path), self.expected))
        self.assertEqual(self.path.read_text(), "replacement")

    def test_direct_policy_age_root_and_admin(self):
        with patch.object(app, "direct_roots", return_value=[str(self.cache)]):
            self.assertTrue(app.direct_eligible(self.cat, str(self.path), self.expected))
            with patch.object(app, "is_admin", return_value=True):
                self.assertFalse(app.direct_eligible(self.cat, str(self.path), self.expected))
            os.utime(self.path, None)
            self.assertFalse(app.direct_eligible(self.cat, str(self.path), fingerprint(str(self.path))))
        self.assertFalse(app.direct_eligible(self.cat, str(self.path), self.expected))

    def test_policy_download_subdirectories_only(self):
        self.assertEqual(app.direct_roots("recycle-bin"), [])
        self.assertEqual(app.direct_roots("residual"), [])
        self.assertNotIn(os.path.join(app.WINDIR, "Temp"), app.direct_roots("temp-files"))
        self.assertTrue(all("pip" in p.lower() for p in app.direct_roots("python-cache")))
        self.assertTrue(all(p.endswith("_cacache") for p in app.direct_roots("npm-store")))
        self.assertEqual(app.direct_roots("nuget-cache"), [os.path.join(app.LA(), "NuGet", "v3-cache")])
        self.assertEqual(app.direct_roots("electron-cache"), [os.path.join(app.LA(), "electron", "Cache")])
        self.assertEqual(app.direct_roots("playwright-browsers"), [])
        self.assertEqual(app.direct_roots("hf-hub"), [])

    def scan_fixture(self):
        app.SCAN = app.ScanJob([self.cat])
        app.CLEAN = app.CleanJob()
        app.SCAN.start()
        deadline = time.monotonic() + 5
        while app.SCAN.status in ("running", "stopping"):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.01)
        with app.OPERATION_LOCK:
            pass

    def test_http_explicit_confirmation_and_bound_mode(self):
        self.scan_fixture()
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def post(endpoint, body):
            request = urllib.request.Request("http://127.0.0.1:%d/api/%s" % (server.server_port, endpoint),
                data=json.dumps(body).encode(), headers={"Content-Type":"application/json", "X-DeepClean-Token":app.API_TOKEN})
            with urllib.request.urlopen(request, timeout=5) as response:
                return json.load(response)
        try:
            with patch.object(app, "direct_roots", return_value=[str(self.cache)]), patch.object(app.CLEAN, "start", return_value=(True, "")) as start:
                preview = post("clean/preview", {"ids":["sandbox"], "delete_mode":"permanent"})
                self.assertTrue(preview["ok"])
                self.assertEqual(preview["delete_mode"], "permanent")
                token = preview["plan_token"]
                self.assertFalse(post("clean", {"plan_token":token})["ok"])
                self.assertFalse(post("clean", {"plan_token":token, "confirm_permanent":True})["ok"])
                start.assert_not_called()
                token = post("clean/preview", {"ids":["sandbox"], "delete_mode":"permanent"})["plan_token"]
                self.assertTrue(post("clean", {"plan_token":token, "confirm_permanent":True, "delete_mode":"recycle"})["ok"])
                self.assertEqual(start.call_args.args[0]["sandbox"]["delete_mode"], "permanent")
                token = post("clean/preview", {"ids":["sandbox"]})["plan_token"]
                self.assertTrue(post("clean", {"plan_token":token, "confirm_permanent":True, "delete_mode":"permanent"})["ok"])
                self.assertEqual(start.call_args.args[0]["sandbox"]["delete_mode"], "recycle")
                self.assertTrue(self.path.exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)
            app.HTTP_PLANS.clear()

    def test_execution_rechecks_age_and_stop(self):
        with patch.object(app, "direct_roots", return_value=[str(self.cache)]):
            stop = threading.Event()
            stop.set()
            self.assertEqual(app.delete_direct_files(self.cat, [str(self.path)], {str(self.path):self.expected}, stop)[0], [])
            stop.clear()
            os.utime(self.path, None)
            self.assertEqual(app.delete_direct_files(self.cat, [str(self.path)], {str(self.path):self.expected}, stop)[0], [])
            self.assertTrue(self.path.exists())

    def test_unsupported_and_locked_categories_rejected(self):
        self.scan_fixture()
        self.assertIsNone(app.build_clean_plan(["sandbox"], delete_mode="permanent")[0])
        with patch.object(app, "direct_roots", return_value=[str(self.cache)]), patch.dict(self.cat, locked=True):
            self.assertIsNone(app.build_clean_plan(["sandbox"], delete_mode="permanent")[0])

    def test_plan_binds_mode_and_skips_new_file(self):
        (self.cache / "new.tmp").write_text("keep")
        self.scan_fixture()
        deadline = time.monotonic() + 5
        with patch.object(app, "direct_roots", return_value=[str(self.cache)]), patch.object(app, "running_processes", return_value=set()):
            plan, error = app.build_clean_plan(["sandbox"], delete_mode="permanent")
            self.assertIsNotNone(plan, error)
            self.assertEqual(len(plan["sandbox"]["entries"]), 1)
            self.assertTrue(app.CLEAN.start(plan, False)[0])
            while app.CLEAN.status in ("running", "stopping"):
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.01)
            with app.OPERATION_LOCK:
                pass
        self.assertFalse(self.path.exists())
        self.assertTrue((self.cache / "new.tmp").exists())
        self.assertEqual(app.CLEAN.snapshot()["via"], "permanent")


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(ROOT)
