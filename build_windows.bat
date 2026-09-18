@echo off
REM ===========================================================================
REM  Easy Life - build the Windows app with ONE double-click.
REM
REM  What this produces (in the "dist" folder):
REM     dist\Easy Life.exe                    portable single file
REM     dist\Easy Life\                       app folder (what the installer ships)
REM     dist\Easy-Life-Setup-<version>.exe    the installer your brother runs
REM     dist\Easy-Life-<version>-update.zip   for in-app updates
REM
REM  Run it by double-clicking. It checks for everything it needs and, if
REM  something is missing, tells you in plain words what to install.
REM
REM  NOTE: a Windows .exe can ONLY be built on Windows. This cannot be done on a
REM  Mac or on Linux, and it cannot be done by anyone working remotely on a Linux
REM  box - the finished .exe has to come from a Windows machine or from the
REM  GitHub Actions release workflow (.github\workflows\release.yml).
REM ===========================================================================
setlocal enabledelayedexpansion
title Easy Life - Build
cd /d "%~dp0"

echo.
echo  ==========================================================
echo    Easy Life  -  build the Windows app
echo  ==========================================================
echo.
echo  This builds the app and, if Inno Setup is installed, the
echo  double-click installer as well. The first run takes a few
echo  minutes (it downloads the build tools once).
echo.

REM A network share (\\server\share) breaks PyInstaller and robocopy in confusing
REM ways - warn early rather than half way through.
if "!CD:~0,2!"=="\\" (
  echo  [Warning] This folder is on a network share. Please copy the whole
  echo            project to a local drive ^(for example C:\pc-rituals^) first.
  echo.
)

REM ---------------------------------------------------------------------------
REM  1. Find Python
REM ---------------------------------------------------------------------------
echo  [1/6] Looking for Python...
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY goto :need_python

REM The Microsoft Store "python" stub exists but does nothing - prove it runs.
for /f "delims=" %%v in ('%PY% --version 2^>^&1') do set "PYVER=%%v"
echo         Found: !PYVER!
%PY% -c "import sys;raise SystemExit(0 if sys.version_info>=(3,10) else 1)" 2>nul
if errorlevel 1 goto :old_python

REM ---------------------------------------------------------------------------
REM  2. Build environment (kept separate from the app's own .venv so building
REM     never disturbs a working install)
REM ---------------------------------------------------------------------------
echo  [2/6] Preparing the build environment...
set "VENV=.venv-build"
set "BPY=!CD!\!VENV!\Scripts\python.exe"
if not exist "!BPY!" (
  echo         Creating !VENV! ^(one time, about a minute^)...
  %PY% -m venv "!VENV!" || goto :venv_failed
)
echo         Installing what the app needs...
"!BPY!" -m pip install --upgrade pip --quiet --disable-pip-version-check || goto :deps_failed
"!BPY!" -m pip install -r requirements.txt --quiet --disable-pip-version-check || goto :deps_failed
echo         Installing PyInstaller ^(the tool that makes the .exe^)...
"!BPY!" -m pip install pyinstaller --quiet --disable-pip-version-check || goto :deps_failed

REM Read the app version straight from the source, so the installer and the
REM update file always agree with what the app reports about itself.
set "APPVER="
for /f "delims=" %%v in ('"!BPY!" -c "import pcrituals;print(pcrituals.__version__)"') do set "APPVER=%%v"
if not defined APPVER (
  echo         [Warning] Could not read the app version - the installer step
  echo                   will be skipped.
)

REM ---------------------------------------------------------------------------
REM  3. Portable single-file .exe
REM ---------------------------------------------------------------------------
echo  [3/6] Building the portable single file...
set "PCRITUALS_TARGET=portable"
"!BPY!" -m PyInstaller --noconfirm --clean pcrituals.spec || goto :build_failed
if not exist "dist\Easy Life.exe" goto :build_failed
echo         Done: dist\Easy Life.exe

REM ---------------------------------------------------------------------------
REM  4. App folder (this is what the installer ships, and what the in-app
REM     updater replaces in place)
REM ---------------------------------------------------------------------------
echo  [4/6] Building the app folder...
set "PCRITUALS_TARGET=installed"
"!BPY!" -m PyInstaller --noconfirm --clean pcrituals.spec || goto :build_failed
if not exist "dist\Easy Life\Easy Life.exe" goto :build_failed

REM ---------------------------------------------------------------------------
REM  5. Check the bundles really contain the app's user interface. This is the
REM     check that catches the "blank window" mistake before you ship it.
REM ---------------------------------------------------------------------------
echo  [5/6] Checking both builds contain everything the app needs...
"!BPY!" scripts\verify_bundle.py "dist\Easy Life" || goto :verify_failed
"!BPY!" scripts\verify_bundle.py "dist\Easy Life.exe" || goto :verify_failed

REM Start the built app once and ask it for its own UI files over HTTP. This is
REM the check that catches a packaged app that crashes at launch (the two crashes
REM that already shipped: a missing stdlib module, and uvicorn logging with no
REM console). A Easy Life window opens for a few seconds and closes by itself.
echo         Starting the built app once to prove it really works...
"!BPY!" scripts\verify_bundle.py "dist\Easy Life" --smoke --timeout 90 || goto :verify_failed

REM ---------------------------------------------------------------------------
REM  6. The installer (needs Inno Setup, a free installer maker)
REM ---------------------------------------------------------------------------
set "INSTALLER_OK=0"
echo  [6/6] Looking for Inno Setup (the installer maker)...
set "PF86=%ProgramFiles(x86)%"
set "PF64=%ProgramFiles%"
set "ISCC="
if exist "%PF86%\Inno Setup 6\ISCC.exe" set "ISCC=%PF86%\Inno Setup 6\ISCC.exe"
if exist "%PF64%\Inno Setup 6\ISCC.exe" set "ISCC=%PF64%\Inno Setup 6\ISCC.exe"
if not defined ISCC (
  for /f "delims=" %%i in ('where iscc 2^>nul') do set "ISCC=%%i"
)

if not defined ISCC (
  echo.
  echo         Inno Setup is NOT on this PC, so the one-click installer was
  echo         skipped. The portable app was still built - see the summary below.
  echo.
  echo         To also build the setup file, install Inno Setup 6 (free, 5 MB):
  echo            easiest:  winget install -e --id JRSoftware.InnoSetup
  echo            or download: https://jrsoftware.org/isdl.php
  echo         Then run this file again - nothing else is missing.
  echo.
) else (
  if not defined APPVER (
    echo         Skipped: the app version could not be read.
  ) else (
    echo         Found: !ISCC!
    echo         Building dist\Easy-Life-Setup-!APPVER!.exe ...
    "!ISCC!" /DAppVersion=!APPVER! "installer\pc-rituals.iss" || goto :inno_failed
    if not exist "dist\Easy-Life-Setup-!APPVER!.exe" goto :inno_failed
    set "INSTALLER_OK=1"
    echo         Done: dist\Easy-Life-Setup-!APPVER!.exe
  )
)

REM ---------------------------------------------------------------------------
REM  Extras for the in-app updater: a .zip of the app folder (that is exactly
REM  what "Update now" inside the app downloads) and its SHA256, which goes into
REM  the update manifest so the download is verified.
REM ---------------------------------------------------------------------------
if defined APPVER (
  echo.
  echo  Packaging dist\Easy-Life-!APPVER!-update.zip for in-app updates...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Compress-Archive -Path 'dist\Easy Life\*' -DestinationPath 'dist\Easy-Life-!APPVER!-update.zip' -Force" >nul 2>&1
  if exist "dist\Easy-Life-!APPVER!-update.zip" (
    echo         SHA256 of that zip ^(needed for the update manifest:^)
    certutil -hashfile "dist\Easy-Life-!APPVER!-update.zip" SHA256 | findstr /r /v "hash CertUtil"
  ) else (
    echo         [Warning] Could not create the update zip. Installer and app are fine.
  )
)

REM ---------------------------------------------------------------------------
REM  Summary
REM ---------------------------------------------------------------------------
echo.
echo  ==========================================================
echo    BUILD COMPLETE
echo  ==========================================================
echo.
echo   Portable app     : dist\Easy Life.exe
echo   App folder       : dist\Easy Life\
if "!INSTALLER_OK!"=="1" echo   INSTALLER        : dist\Easy-Life-Setup-!APPVER!.exe   ^<-- give this to your brother
if "!INSTALLER_OK!"=="0" echo   INSTALLER        : not built ^(install Inno Setup - see above^)
if defined APPVER echo   In-app update    : dist\Easy-Life-!APPVER!-update.zip
echo.
echo   What to hand over:
if "!INSTALLER_OK!"=="1" (
  echo     ONE file: dist\Easy-Life-Setup-!APPVER!.exe
  echo     He double-clicks it, clicks Next twice, and Easy Life is installed
  echo     with a Start Menu entry, a desktop shortcut and an uninstaller.
) else (
  echo     The portable file dist\Easy Life.exe - he can just double-click it.
  echo     Nothing to install, no Python needed.
)
echo.
echo   Windows will show a blue "Windows protected your PC" warning the first
echo   time, because the app is not code-signed. Click "More info" then
echo   "Run anyway". Signing needs a paid certificate - see BUILD.md.
echo.
echo   Full details: BUILD.md
echo.
pause
endlocal
exit /b 0

REM ===========================================================================
REM  Friendly failure messages. Each one says exactly what to do next.
REM ===========================================================================

:need_python
echo.
echo  [MISSING] Python is not installed on this PC.
echo.
echo    1. Go to https://www.python.org/downloads/ and download Python 3.12
echo       (any version from 3.10 up works).
echo    2. Run the installer and TICK THE BOX "Add python.exe to PATH" on the
echo       very first screen. This step is easy to miss and everything here
echo       depends on it.
echo    3. Close this window and run build_windows.bat again.
echo.
pause
endlocal
exit /b 1

:old_python
echo.
echo  [TOO OLD] !PYVER! is older than Python 3.10.
echo    Install a newer Python from https://www.python.org/downloads/
echo    (tick "Add python.exe to PATH" during setup) and try again.
echo.
pause
endlocal
exit /b 1

:venv_failed
echo.
echo  [FAILED] Could not create the build environment in .venv-build.
echo    - Close any other build window and try again.
echo    - If antivirus blocked it, allow "python.exe" for this folder.
echo.
pause
endlocal
exit /b 1

:deps_failed
echo.
echo  [FAILED] Downloading the app's libraries failed.
echo    - Check that this PC is online.
echo    - On a work network, a proxy may be blocking python.org/pypi.org.
echo    - Try again; a dropped connection is the usual cause.
echo.
pause
endlocal
exit /b 1

:build_failed
echo.
echo  [FAILED] PyInstaller could not build the app. Read the red text above.
echo    Two things cause this almost every time:
echo      - Antivirus quarantined a file while it was being packed. Add an
echo        exclusion for this project folder and run this file again.
echo      - The previous build was interrupted: delete the "build" and "dist"
echo        folders, then run this file again.
echo.
pause
endlocal
exit /b 1

:verify_failed
echo.
echo  [FAILED] The app was built but the check found something missing in it.
echo    The message above names the missing file. Most common cause: antivirus
echo    deleted part of the bundle while it was being packed - add an exclusion
echo    for this folder, delete "build" and "dist", and run this file again.
echo.
pause
endlocal
exit /b 1

:inno_failed
echo.
echo  [FAILED] Inno Setup reported an error while making the installer
echo    (the message above comes from Inno Setup itself).
echo    The app itself built fine - the summary below the error still applies.
echo    For help: https://jrsoftware.org/ishelp/
echo.
pause
endlocal
exit /b 1
