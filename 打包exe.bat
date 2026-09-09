@echo off
cd /d "%~dp0"
if not exist "build\release-env\Scripts\python.exe" python -m venv build\release-env
if errorlevel 1 exit /b 1
build\release-env\Scripts\python.exe -m pip install -r requirements-build.txt
if errorlevel 1 exit /b 1
build\release-env\Scripts\python.exe scripts\build_release.py
if errorlevel 1 exit /b 1
echo Candidate created in dist\candidate. Not signed or published.
pause
