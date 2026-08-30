@echo off
rem Build DeepClean single-file exe. Needs Python 3.8+ and pip network access.
rem NOTE: keep this file ASCII-only. A batch file with "chcp 65001" plus
rem multibyte text can misparse later lines (classic cmd codepage offset bug).
cd /d "%~dp0"
echo [1/3] Installing PyInstaller...
python -m pip install --quiet pyinstaller || (echo PyInstaller install failed & pause & exit /b 1)
echo [2/3] Building (about 1-2 minutes)...
python -m PyInstaller --noconfirm --onefile --noconsole ^
  --name DeepClean ^
  --add-data "static;static" ^
  --add-data "rules;rules" ^
  app.py || (echo Build failed & pause & exit /b 1)
echo [3/3] Done: dist\DeepClean.exe
echo Double-click DeepClean.exe to run. No Python needed on the target machine.
pause
