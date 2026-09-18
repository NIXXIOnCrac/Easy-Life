@echo off
REM Easy Life - Uninstaller. Removes shortcuts, firewall rule, and data.
setlocal
echo.
echo  Easy Life Uninstaller
echo  ======================
echo  This will remove the desktop/start-menu shortcuts and the firewall rule.
echo  Your data (rituals, pairings, backups) lives in:
echo     %LOCALAPPDATA%\Easy Life\data
echo  It will NOT be deleted unless you choose to delete the whole folder.
echo.
set /p ANS=Remove shortcuts, firewall rule, and install folder too? [y/N]: 
if /i not "%ANS%"=="y" (
  echo  Removing shortcuts and firewall rule only...
  del "%USERPROFILE%\Desktop\Easy Life.lnk" >nul 2>&1
  del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Easy Life.lnk" >nul 2>&1
  netsh advfirewall firewall delete rule name="Easy Life" >nul 2>&1
  echo  Done. Your data was kept.
  pause
  exit /b
)

echo.
echo  Removing everything...
taskkill /IM python.exe /FI "WINDOWTITLE eq Easy Life*" >nul 2>&1
del "%USERPROFILE%\Desktop\Easy Life.lnk" >nul 2>&1
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Easy Life.lnk" >nul 2>&1
netsh advfirewall firewall delete rule name="Easy Life" >nul 2>&1

set "INSTALL_DIR="
if exist "%PUBLIC%\Easy Life install.txt" set /p INSTALL_DIR=<"%PUBLIC%\Easy Life install.txt"
if "%INSTALL_DIR%"=="" set "INSTALL_DIR=%LOCALAPPDATA%\Easy Life"

if exist "%INSTALL_DIR%" (
  rmdir /s /q "%INSTALL_DIR%"
  echo  Deleted "%INSTALL_DIR%"
) else (
  echo  Install folder not found; skipped.
)

echo  Easy Life has been uninstalled.
pause