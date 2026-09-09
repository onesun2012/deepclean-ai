"""Build a review candidate and record its inputs; no publishing or signing."""
import hashlib
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
destination = root / "dist" / "candidate"
subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--noconsole",
                "--name", "DeepClean", "--icon", str(root / "assets" / "icon.ico"),
                "--add-data", str(root / "static") + ";static", "--add-data", str(root / "rules") + ";rules", "--add-data", str(root / "support.json") + ";.",
                "--distpath", str(destination), "--workpath", str(root / "build" / "candidate"),
                "--specpath", str(root / "build" / "spec"), "app.py"], cwd=root, check=True)
inputs = [root / name for name in ("app.py", "safety.py", "instance.py", "support.json", "requirements-build.txt", "assets/icon.ico")]
inputs += list((root / "rules").glob("*.json")) + list((root / "static").rglob("*"))
digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
manifest = dict(python=sys.version, platform=platform.platform(),
                inputs={p.relative_to(root).as_posix():digest(p) for p in sorted(inputs) if p.is_file()},
                executable_sha256=digest(destination / "DeepClean.exe"), signed=False)
(destination / "build-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
(destination / "SHA256SUMS.txt").write_text(manifest["executable_sha256"] + "  DeepClean.exe\n", encoding="ascii")
extras = ["LICENSE", "README.md", "docs/RELEASE-CANDIDATE.md", "docs/SITE.md"]
exit_bat = next((p.name for p in root.glob("*.bat") if "退出" in p.name), None)
if exit_bat:
    extras.append(exit_bat)
for name in extras:
    src = root / name
    if src.exists():
        shutil.copy2(src, destination / src.name)
print("Candidate: " + str(destination / "DeepClean.exe"))
