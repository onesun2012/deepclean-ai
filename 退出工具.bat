@echo off
title ClearC Stopper
rem ---- kill the ClearC local server listening on ports 8520-8525 ----
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8520 :8521 :8522 :8523 :8524 :8525" ^| findstr "LISTENING"') do taskkill /PID %%p /F >nul 2>&1
echo [ClearC] Stopped. You can close this window.
ping -n 3 127.0.0.1 >nul
