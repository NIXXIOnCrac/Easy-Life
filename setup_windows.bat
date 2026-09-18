@echo off
REM ===========================================================================
REM  Easy Life - double-click setup for Windows 10/11
REM
REM  Just double-click this file. It walks through everything:
REM
REM    1. Checks Python is installed (and explains what to do if not)
REM    2. Chooses where to install  (default: your own user folder, no admin)
REM    3. Copies the app there
REM    4. Creates its own Python environment and downloads what it needs
REM    5. Starts the app once to prove it works before making shortcuts
REM    6. Makes a Start Menu entry + desktop shortcut, and optionally opens the
REM       firewall so your iPhone can control this PC
REM    7. Launches Easy Life, and offers to build a proper installer (.exe)
REM       you could give to someone else
REM
REM  Nothing here needs administrator rights. The one thing that does - the
REM  firewall rule for phone control - asks for permission on its own, and only
REM  if you say yes to it.
REM
REM  Your rituals, settings and paired phones live in %LOCALAPPDATA%\Easy Life\data.
REM  That folder is never touched or removed by this setup.
REM ===========================================================================
setlocal enabledelayedexpansion
title Easy Life - Setup
cd /d "%~dp0"

REM A trailing backslash makes path comparisons awkward; keep a clean copy.
set "SRC=%~dp0"
if "!SRC:~-1!"=="\" set "SRC=!SRC:~0,-1!"

REM Some PCs have these set globally, which breaks a new virtual environment.
set "PYTHONHOME="
set "PYTHONPATH="
set "PIP_DISABLE_PIP_VERSION_CHECK=1"

echo.
echo  ==========================================================
echo    Easy Life  -  Setup
echo  ==========================================================
echo.
echo   This installs Easy Life for you. It takes a couple of
echo   minutes and you can leave it running.
echo.

REM ---------------------------------------------------------------------------
REM  0. If a ready-made installer is sitting in dist\, offer it first: that is
REM     the finished product, it needs no Python at all, and /SILENT means the
REM     user sees no extra windows.
REM ---------------------------------------------------------------------------
set "SETUPFILE="
for %%f in ("dist\Easy-Life-Setup-*.exe") do set "SETUPFILE=%%~ff"
REM A for-loop with no match returns the literal pattern - drop that case.
if defined SETUPFILE if not exist "!SETUPFILE!" set "SETUPFILE="
if defined SETUPFILE (
  echo   A ready-made installer was found:
  echo      !SETUPFILE!
  echo.
  set /p USEINST=  Install with it? It needs no Python. [Y/n]:
  if /i not "!USEINST!"=="n" (
    echo.
    echo   [1/1] Installing ^(silent, no extra questions^)...
    "!SETUPFILE!" /SILENT /SUPPRESSMSGBOXES /NORESTART
    if errorlevel 1 (
      echo.
      echo   [Note] The installer did not finish ^(code !errorlevel!^).
      echo          That usually means you cancelled it, or an antivirus blocked
      echo          it. Falling back to installing from the source files.
      echo.
    ) else (
      echo.
      echo   ========================================================
      echo    Easy Life is installed.
      echo   ========================================================
      echo.
      echo    Start it from the Start Menu ^(search "Easy Life"^) or the
      echo    desktop shortcut. Your rituals will be kept in:
      echo       %LOCALAPPDATA%\Easy Life\data
      echo.
      pause
      endlocal
      exit /b 0
    )
  )
)

REM ---------------------------------------------------------------------------
REM  1. Python
REM ---------------------------------------------------------------------------
echo  [1/7] Checking for Python...
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY goto :no_python

for /f "delims=" %%v in ('%PY% --version 2^>^&1') do set "PYVER=%%v"
%PY% -c "import sys;raise SystemExit(0 if sys.version_info>=(3,10) else 1)" 2>nul
if errorlevel 1 (
  echo.
  echo  [Problem] !PYVER! is too old. Easy Life needs Python 3.10 or newer.
  echo            Install it from https://www.python.org/downloads/ and tick
  echo            "Add python.exe to PATH". Then run this file again.
  echo.
  pause
  endlocal
  exit /b 1
)
echo         Found !PYVER%. Good.

REM ---------------------------------------------------------------------------
REM  2. Where to install
REM ---------------------------------------------------------------------------
REM Per-user install under %LOCALAPPDATA%\Programs - the same place the proper
REM installer uses. No administrator prompt, and the user's data folder
REM (%LOCALAPPDATA%\Easy Life\data) sits OUTSIDE this folder, so uninstalling
REM the program can never take the rituals with it.
set "INSTALL_DIR=%LOCALAPPDATA%\Programs\Easy Life"
echo  [2/7] Install folder: !INSTALL_DIR!
echo         Press Enter to accept, or type a different folder.
set "CUSTOM="
set /p CUSTOM=         Folder:
if not "!CUSTOM!"=="" set "INSTALL_DIR=!CUSTOM!"
REM Strip surrounding quotes if the user pasted a quoted path.
set INSTALL_DIR=!INSTALL_DIR:"=!
if "!INSTALL_DIR:~-1!"=="\" set "INSTALL_DIR=!INSTALL_DIR:~0,-1!"

if exist "!INSTALL_DIR!\desktop.py" (
  echo         An installed copy is already here - updating it in place.
)

REM ---------------------------------------------------------------------------
REM  3. Copy the application files
REM ---------------------------------------------------------------------------
echo  [3/7] Copying application files...
if not exist "!INSTALL_DIR!" mkdir "!INSTALL_DIR!" || goto :copy_failed

if /i "!SRC!"=="!INSTALL_DIR!" (
  echo         Already running from the install folder - nothing to copy.
) else (
  REM /E copies subfolders. /XD skips build junk, other virtualenvs and git
  REM history; /XF skips compiled leftovers. Robocopy reports 0-7 for success,
  REM and 8 or more for a real failure (so "errorlevel 8" is not an error).
  robocopy "!SRC!" "!INSTALL_DIR!" /E /NFL /NDL /NJH /NJS ^
    /XD .git .venv .venv-build venv dist build __pycache__ .pytest_cache node_modules data backups ^
    /XF *.pyc >nul
  if !errorlevel! GEQ 8 goto :copy_failed
)

REM ---------------------------------------------------------------------------
REM  4. Environment + libraries
REM ---------------------------------------------------------------------------
echo  [4/7] Setting up the Python environment ^(this is the slow bit^)...
set "PYEXE=!INSTALL_DIR!\.venv\Scripts\python.exe"
set "PYWEXE=!INSTALL_DIR!\.venv\Scripts\pythonw.exe"
if not exist "!PYEXE!" (
  %PY% -m venv "!INSTALL_DIR!\.venv" || goto :venv_failed
)
"!PYEXE!" -m pip install --upgrade pip --quiet || goto :deps_failed
"!PYEXE!" -m pip install -r "!INSTALL_DIR!\requirements.txt" --quiet || goto :deps_failed

REM Prove the libraries really import, instead of finding out later from a
REM shortcut that flashes and dies. pushd matters: without it this would import
REM the copy in the folder you are running setup from, not the installed one.
pushd "!INSTALL_DIR!"
"!PYEXE!" -c "import fastapi, uvicorn, pydantic, qrcode; import pcrituals.api"
set "IMPORTRC=!errorlevel!"
popd
if not "!IMPORTRC!"=="0" goto :deps_failed
echo         Libraries installed.

REM ---------------------------------------------------------------------------
REM  5. Start the app once and check it serves its own user interface
REM ---------------------------------------------------------------------------
echo  [5/7] Checking Easy Life starts properly...
pushd "!INSTALL_DIR!"
"!PYEXE!" scripts\verify_bundle.py --smoke-source
set "BOOTRC=!errorlevel!"
popd
if not "!BOOTRC!"=="0" (
  echo.
  echo  [Warning] The test start did not succeed - see the notes above.
  echo            Common causes: another program is using port 8765, or an
  echo            antivirus is blocking Python.
  echo.
  set /p GO=         Install the shortcuts anyway? [y/N]:
  if /i not "!GO!"=="y" (
    echo         Stopped. Nothing was changed. Fix the issue above and re-run.
    pause
    endlocal
    exit /b 1
  )
) else (
  echo         It starts and serves its interface.
)

REM ---------------------------------------------------------------------------
REM  6. Shortcuts + optional firewall rule for phone control
REM ---------------------------------------------------------------------------
echo  [6/7] Creating shortcuts...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ws = New-Object -ComObject WScript.Shell; $ico = '!INSTALL_DIR!\pcrituals\web\icons\pcrituals.ico'; $desk = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Easy Life.lnk'; $lnk = $ws.CreateShortcut($desk); $lnk.TargetPath = '!PYWEXE!'; $lnk.Arguments = 'desktop.py'; $lnk.WorkingDirectory = '!INSTALL_DIR!'; $lnk.Description = 'Easy Life - run your PC rituals'; $lnk.IconLocation = $ico; $lnk.Save(); $menu = Join-Path ([Environment]::GetFolderPath('Programs')) 'Easy Life.lnk'; $m = $ws.CreateShortcut($menu); $m.TargetPath = '!PYWEXE!'; $m.Arguments = 'desktop.py'; $m.WorkingDirectory = '!INSTALL_DIR!'; $m.Description = 'Easy Life - run your PC rituals'; $m.IconLocation = $ico; $m.Save()" 2>nul
if errorlevel 1 (
  echo         [Note] The Start Menu / desktop shortcuts could not be created
  echo                automatically. You can still start the app with the
  echo                shortcuts inside !INSTALL_DIR!
) else (
  echo         Start Menu: "Easy Life"    Desktop: "Easy Life"
)

echo.
echo         Easy Life can be controlled from your iPhone on the same Wi-Fi.
echo         That needs one Windows Firewall rule (one permission prompt).
set /p FW=         Add the firewall rule now? [Y/n]:
if /i not "!FW!"=="n" (
  echo.
  REM No "quiet" here: if it fails, the instructions stay on screen a moment.
  call "!INSTALL_DIR!\scripts\add_firewall_rule.bat"
  if errorlevel 1 (
    echo         [Note] The firewall rule was not added. Easy Life still works
    echo                on this PC; only the phone control needs it.
  )
)

REM Remember where we installed, so uninstall_windows.bat can find it.
> "%PUBLIC%\Easy Life install.txt" echo !INSTALL_DIR! 2>nul

REM ---------------------------------------------------------------------------
REM  7. Launch + offer to build the finished installer
REM ---------------------------------------------------------------------------
echo  [7/7] Starting Easy Life...
start "" /D "!INSTALL_DIR!" "!PYWEXE!" desktop.py

echo.
echo  ==========================================================
echo    Easy Life is installed
echo  ==========================================================
echo.
echo    Start it any time from the Start Menu or the desktop icon.
echo.
echo    Where things live:
echo      Program : !INSTALL_DIR!
echo      Your data (rituals, settings, paired phones):
echo                %LOCALAPPDATA%\Easy Life\data
echo      Uninstall: !INSTALL_DIR!\uninstall_windows.bat
echo                 ^(your data is kept^)
echo.
echo    Tip: the first time you open it, Easy Life asks you to
echo    create a username and password.
echo.

set /p BUILDIT=  Build the finished installer .exe (needs Inno Setup) to give to someone else? [y/N]:
if /i "!BUILDIT!"=="y" (
  echo.
  echo  Starting the build. Read the output - it says exactly what to install
  echo  if anything is missing.
  echo.
  call "!SRC!\build_windows.bat"
)

echo.
echo  All done. You can close this window.
pause
endlocal
exit /b 0

REM ===========================================================================
REM  Friendly problem explanations
REM ===========================================================================

:no_python
echo.
echo  [Python is not installed]
echo.
echo   Easy Life needs Python 3.10 or newer. Installing it takes 2 minutes:
echo.
echo     1. Open https://www.python.org/downloads/
echo     2. Download Python 3.12 for Windows.
echo     3. On the FIRST screen of the installer, TICK the box that says
echo            "Add python.exe to PATH"
echo        This is easy to miss and everything here depends on it.
echo     4. Click "Install Now", wait, then double-click this file again.
echo.
pause
endlocal
exit /b 1

:copy_failed
echo.
echo  [Could not copy the files]
echo   Nothing was installed. Try this:
echo     - Close any other Easy Life window and run this file again.
echo     - Pick a different install folder when it asks (step 2).
echo     - If your antivirus blocked it, allow this folder and retry.
echo.
pause
endlocal
exit /b 1

:venv_failed
echo.
echo  [Could not create the Python environment]
echo   Try again, and if it keeps failing:
echo     - Install Python from python.org rather than the Microsoft Store.
echo     - Allow "python.exe" in your antivirus for this folder.
echo.
pause
endlocal
exit /b 1

:deps_failed
echo.
echo  [Downloading the app's libraries failed]
echo   Easy Life needs to download some free libraries the first time.
echo     - Check the PC is online.
echo     - On a work network a proxy may block python.org / pypi.org.
echo     - Run this file again - a dropped connection is the usual cause.
echo.
pause
endlocal
exit /b 1
