@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  where py >nul 2>nul
  if errorlevel 1 (
    python -m venv .venv
  ) else (
    py -3 -m venv .venv
  )
  if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -m pip install -e ".[windows,ocr]"
if errorlevel 1 goto failed
echo Setup complete. Open Start-Test-Console.cmd for simulation.
pause
exit /b 0
:failed
echo Setup failed. Python 3.11+ is required. See README.md.
pause
exit /b 1
