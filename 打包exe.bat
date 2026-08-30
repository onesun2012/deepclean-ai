@echo off
rem 一键打包深清 DeepClean 为单文件 exe（需要 Python 与网络：pip install pyinstaller）
chcp 65001 >nul
cd /d "%~dp0"
echo [1/3] 安装 PyInstaller...
python -m pip install --quiet pyinstaller || (echo PyInstaller 安装失败 & pause & exit /b 1)
echo [2/3] 打包中（约 1-2 分钟）...
python -m PyInstaller --noconfirm --onefile --noconsole ^
  --name DeepClean ^
  --add-data "static;static" ^
  --add-data "rules;rules" ^
  app.py || (echo 打包失败 & pause & exit /b 1)
echo [3/3] 完成: dist\DeepClean.exe
echo 双击 DeepClean.exe 即可运行，无需安装 Python。
pause
