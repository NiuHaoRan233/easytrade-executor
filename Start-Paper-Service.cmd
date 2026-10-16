@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -X utf8 -m easytrade.launcher --mode paper
) else if exist ".venv-ocr-probe\Scripts\python.exe" (
  ".venv-ocr-probe\Scripts\python.exe" -X utf8 -m easytrade.launcher --mode paper
) else (
  echo Run Setup.cmd first.
)
pause
