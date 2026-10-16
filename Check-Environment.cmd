@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -X utf8 tools\check_environment.py %*
) else (
  where py >nul 2>nul
  if errorlevel 1 (
    python -X utf8 tools\check_environment.py %*
  ) else (
    py -3 -X utf8 tools\check_environment.py %*
  )
)
pause
