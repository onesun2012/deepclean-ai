"""CLI smoke tests restricted to one temporary cache root."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

app = Path(__file__).resolve().parents[1] / "app.py"
with tempfile.TemporaryDirectory(prefix="deepclean_cli_") as root:
    cache = Path(root) / "cache"
    cache.mkdir()
    (cache / "junk.txt").write_text("junk")
    env = dict(os.environ, PYTHONIOENCODING="utf-8", CLEAR_C_SANDBOX=str(cache), DEEPCLEAN_HISTORY=str(Path(root) / "history.jsonl"), DEEPCLEAN_RUNTIME_DIR=str(Path(root) / "runtime"))
    def run(*args):
        result = subprocess.run([sys.executable, str(app), "cli", *args], env=env,
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        return result.returncode, json.loads(result.stdout)
    code, result = run("scan")
    assert code == 0, result
    code, result = run("clean", "--ids", "sandbox")
    assert code == 2 and (cache / "junk.txt").exists(), result
    code, result = run("clean", "--ids", "sandbox", "--dry")
    assert code == 0 and (cache / "junk.txt").exists(), result
    code, result = run("move", "--tool", "ollama", "--to", "D")
    assert code == 1, result
print("4 isolated CLI checks passed")
