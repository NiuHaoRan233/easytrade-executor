@echo off
setlocal
cd /d "%~dp0"
set "ET_NO_PAUSE=0"
set "ET_SKIP_OCR=0"
for %%A in (%*) do (
  if "%%~A"=="--no-pause" set "ET_NO_PAUSE=1"
  if "%%~A"=="--skip-ocr-warmup" set "ET_SKIP_OCR=1"
)
echo [1/4] Checking Python. Python 3.11+ with Tcl/Tk is required.
if exist ".venv\Scripts\python.exe" (
  echo [2/4] Reusing the project's .venv.
  goto environment_ready
)
where py >nul 2>nul
if errorlevel 1 goto python_path
py -3.12 -c "import sys; assert sys.version_info >= (3,11)" >nul 2>nul
if errorlevel 1 goto default_py
echo [2/4] Creating .venv using Python 3.12.
py -3.12 -m venv .venv
if errorlevel 1 goto failed
goto environment_ready
:default_py
py -3 -c "import sys; assert sys.version_info >= (3,11)" >nul 2>nul
if errorlevel 1 goto failed
echo [2/4] Creating .venv using the Python launcher.
py -3 -m venv .venv
if errorlevel 1 goto failed
goto environment_ready
:python_path
python -c "import sys; assert sys.version_info >= (3,11)" >nul 2>nul
if errorlevel 1 goto failed
echo [2/4] Creating .venv using Python on PATH.
python -m venv .venv
if errorlevel 1 goto failed
:environment_ready
".venv\Scripts\python.exe" -c "import sys, tkinter; assert sys.version_info >= (3,11); tkinter.Tcl()"
if errorlevel 1 goto failed
echo [3/4] Installing dependencies. First installation downloads large packages.
set "ET_CONSTRAINTS="
".venv\Scripts\python.exe" -c "import sys; raise SystemExit(sys.version_info[:2] != (3,13))"
if not errorlevel 1 set "ET_CONSTRAINTS=-c constraints\windows-py313-tested.txt"
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --only-binary=:all: %ET_CONSTRAINTS% -e ".[windows,ocr]"
if errorlevel 1 goto failed
if "%ET_SKIP_OCR%"=="1" goto skip_ocr
echo [4/4] Checking OCR models on a blank image. No trading client is accessed.
".venv\Scripts\python.exe" -X utf8 -m easytrade.doctor --warm-ocr
if errorlevel 1 goto failed
goto complete
:skip_ocr
echo [4/4] OCR warmup skipped. First OCR read may initialize or download models.
:complete
echo Setup complete. Next: Check-Environment.cmd, then Configure-Client.cmd.
echo Read docs\first-machine.md before live use. Setup never places an order.
if "%ET_NO_PAUSE%"=="0" pause
exit /b 0
:failed
echo Setup failed. Read the error above, then run Check-Environment.cmd.
echo Python 3.11+ with Tcl/Tk is required. See docs\first-machine.md.
if "%ET_NO_PAUSE%"=="0" pause
exit /b 1
