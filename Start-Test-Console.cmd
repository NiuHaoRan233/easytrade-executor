@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -X utf8 -m easytrade.console
) else if exist ".venv-ocr-probe\Scripts\python.exe" (
  ".venv-ocr-probe\Scripts\python.exe" -X utf8 -m easytrade.console
) else (
  echo Python environment not found. See README.md for installation.
  pause
  exit /b 1
)
if errorlevel 1 pause
