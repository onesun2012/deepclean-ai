@echo off
cd /d "%~dp0"
title ClearC Launcher

set "PYW="
for /f "delims=" %%i in ('where pythonw 2^>nul') do if not defined PYW set "PYW=%%i"

if defined PYW (
    start "" "%PYW%" "%~dp0app.py"
) else (
    echo [ClearC] pythonw not found, trying python ...
    where python >nul 2>nul
    if errorlevel 1 (
        echo [ClearC] ERROR: Python not found. Please install Python 3.8+ first.
        pause
        exit /b 1
    )
    start "" python "%~dp0app.py"
)

rem ---- wait until the local server answers (max ~12s) ----
set /a TRIES=0
:waitloop
set /a TRIES+=1
curl -s -m 1 -o nul http://127.0.0.1:8520/ >nul 2>&1 && goto :started
if %TRIES% GEQ 12 goto :failed
ping -n 2 127.0.0.1 >nul
goto :waitloop

:started
echo [ClearC] Started OK. A browser page should open automatically.
echo [ClearC] You can close this window now.
ping -n 3 127.0.0.1 >nul
exit /b 0

:failed
echo [ClearC] ERROR: server did not start within 12 seconds.
echo [ClearC] Details: see clearc.log in this folder.
pause
exit /b 1
