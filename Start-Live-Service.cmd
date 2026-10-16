@echo off
setlocal
cd /d "%~dp0"
if not exist "config\ths.local.json" (
  echo Copy config\ths.example.json to config\ths.local.json and configure your client first.
  pause
  exit /b 1
)
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -X utf8 -m easytrade.launcher --mode ths-live
) else if exist ".venv-ocr-probe\Scripts\python.exe" (
  ".venv-ocr-probe\Scripts\python.exe" -X utf8 -m easytrade.launcher --mode ths-live
) else (
  echo Run Setup.cmd first.
)
pause
