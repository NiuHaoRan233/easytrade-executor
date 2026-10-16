@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run Setup.cmd first. See docs\first-machine.md.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -X utf8 -m easytrade.doctor --init-config %*
set "ET_CONFIG_EXIT=%ERRORLEVEL%"
pause
exit /b %ET_CONFIG_EXIT%
