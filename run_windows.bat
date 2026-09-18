@echo off
REM Easy Life - quick run (no install). Opens the native app window.
setlocal
cd /d "%~dp0"

set "VENV=.venv"
set "PYTHON=%VENV%\Scripts\python.exe"

if not exist "%VENV%" (
  echo [Easy Life] First run - creating virtual environment...
  python -m venv "%VENV%" || (echo Failed to create venv. Install Python 3.10+ and retry. & pause & exit /b 1)
)

if not exist "%VENV%\Lib\site-packages\fastapi" (
  echo [Easy Life] Installing dependencies...
  "%PYTHON%" -m pip install --upgrade pip >nul 2>&1
  "%PYTHON%" -m pip install -r requirements.txt || (echo Dependency install failed. & pause & exit /b 1)
  "%PYTHON%" -m pip install pywebview >nul 2>&1
)

echo [Easy Life] Starting...
REM Native app window (falls back to the browser if WebView2 is unavailable).
"%VENV%\Scripts\pythonw.exe" desktop.py
if errorlevel 1 (
  "%PYTHON%" -m pcrituals
  pause
)
endlocal