@echo off
cd /d "%~dp0"
title ClearC Launcher (Admin)

rem ---- relaunch app.py elevated (UAC prompt will appear) ----
powershell -NoProfile -Command "$pyw=(Get-Command pythonw -ErrorAction SilentlyContinue).Source; if(-not $pyw){$pyw=(Get-Command python -ErrorAction SilentlyContinue).Source}; if($pyw){Start-Process -FilePath $pyw -ArgumentList ('\"' + '%~dp0app.py' + '\"') -WorkingDirectory '%~dp0' -Verb RunAs} else {Write-Host 'Python not found'}"

rem ---- wait until the local server answers (max ~15s) ----
set /a TRIES=0
:waitloop
set /a TRIES+=1
curl -s -m 1 -o nul http://127.0.0.1:8520/ >nul 2>&1 && goto :started
if %TRIES% GEQ 15 goto :failed
ping -n 2 127.0.0.1 >nul
goto :waitloop

:started
echo [ClearC] Admin instance started (UAC confirmed). A browser page should open.
echo [ClearC] You can close this window now.
ping -n 3 127.0.0.1 >nul
exit /b 0

:failed
echo [ClearC] ERROR: admin instance did not start (UAC declined, or blocked by antivirus).
echo [ClearC] Details: see clearc.log in this folder.
pause
exit /b 1
