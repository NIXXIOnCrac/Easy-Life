@echo off
REM Run the full Easy Life test suite (backend + frontend).
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Create the venv first:  python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
  pause & exit /b 1
)

set PY=.venv\Scripts\python.exe
set FAIL=0

echo === Backend tests ===
"%PY%" -m pytest tests -q || set FAIL=1

echo.
echo === Frontend tests ===
where node >nul 2>nul
if errorlevel 1 (
  echo (node not installed - skipping frontend tests^)
) else (
  for %%T in (smoke_frontend deck_render_test phone_pair_test onboarding_ui_test frontend_regression_test deep_pages_test phone_e2e_test live_e2e_test) do (
    echo --- %%T ---
    node tests\%%T.js || set FAIL=1
  )
)

echo.
if "%FAIL%"=="0" (echo ALL TESTS PASSED) else (echo SOME TESTS FAILED)
pause
endlocal