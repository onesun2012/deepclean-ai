@echo off
cd /d "%~dp0"
if exist "%~dp0DeepClean.exe" (
  start "" /wait "%~dp0DeepClean.exe" --stop
  exit /b
)
python "%~dp0app.py" --stop
if errorlevel 1 pause
