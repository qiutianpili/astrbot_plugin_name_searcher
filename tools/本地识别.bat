@echo off
chcp 65001 >nul
cd /d "%~dp0"
if "%~1"=="" (
  echo 把要识别的文件夹拖到这个 .bat 上即可。
  pause
  exit /b 1
)
python name_searcher_local.py run "%~1"
pause
