@echo off
cd /d "%~dp0"
set "DEEPCLEAN_LAUNCH_PATH=%~dp0app.py"
powershell -NoProfile -Command "$py=(Get-Command pythonw -ErrorAction Stop).Source; Start-Process -FilePath $py -ArgumentList ('\"' + $env:DEEPCLEAN_LAUNCH_PATH + '\"') -Verb RunAs -WindowStyle Hidden"
