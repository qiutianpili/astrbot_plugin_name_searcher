@echo off
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw name_searcher_gui.py %*
) else (
  python name_searcher_gui.py %*
  if errorlevel 1 pause
)
