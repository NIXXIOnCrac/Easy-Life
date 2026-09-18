@echo off
REM ===========================================================================
REM  Easy Life - open the Windows Firewall port the phone app talks to.
REM
REM  Why this is a separate file: only THIS step needs administrator rights.
REM  setup_windows.bat and the installer call it, it asks Windows for the rights
REM  itself (one prompt, "Do you want to allow this app to make changes?"), and
REM  everything else stays unprivileged.
REM
REM  Usage:  add_firewall_rule.bat          (asks for rights, then adds the rule)
REM          add_firewall_rule.bat quiet    (same, but no closing pause)
REM ===========================================================================
setlocal
REM Harmless when launched from anywhere (a folder with spaces included).
cd /d "%~dp0"
set "PORT=8765"
set "RC=0"

REM --- Are we already running with administrator rights? --------------------
net session >nul 2>&1
if %errorlevel%==0 goto :add_rule

REM Not elevated. If this copy was ALREADY the elevated one, elevation failed
REM (the user clicked "No" on the Windows prompt) - do not loop forever.
if /i "%~1"=="elevated" goto :elevation_failed

echo  Asking Windows for permission to change the firewall (one prompt)...
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList 'elevated %*' -Verb RunAs -Wait" 2>nul
if errorlevel 1 goto :elevation_failed
goto :the_end

:add_rule
echo  Allowing port %PORT% through the Windows Firewall for Easy Life...
REM Replace any old rule so the settings never stack up.
netsh advfirewall firewall delete rule name="Easy Life" >nul 2>&1
REM "profile=any" matters: home Wi-Fi is often classified as a Public network,
REM which is why Easy Life' port gets blocked by default.
netsh advfirewall firewall add rule name="Easy Life" dir=in action=allow protocol=TCP localport=%PORT% profile=any >nul 2>&1
if errorlevel 1 goto :manual
echo  [OK] Firewall rule added - your iPhone can reach this PC on port %PORT%.
goto :the_end

:elevation_failed
set "RC=1"
echo.
echo  [Note] Windows did not give permission, so the firewall was left alone.
echo         Without the rule, Easy Life still works on this PC, but your
echo         iPhone cannot reach it over Wi-Fi.
echo.
echo  To add it by hand later:
echo    1. Open "Windows Security" - "Firewall ^& network protection"
echo    2. Click "Advanced settings" - "Inbound Rules" - "New Rule..."
echo    3. Choose "Port", TCP, port %PORT%, "Allow the connection"
echo    4. Name it "Easy Life" and click Finish.
goto :the_end

:manual
set "RC=1"
echo  [Note] Could not add the firewall rule automatically.
echo         Add it by hand: Windows Security, Firewall, Advanced settings,
echo         Inbound Rules, New rule, Port, TCP %PORT%, Allow, name it "Easy Life".

:the_end
REM From the installer this runs hidden; from a wizard a short pause is friendly.
echo %* | find /i "quiet" >nul
if errorlevel 1 (
  echo.
  echo  (This window closes by itself in 12 seconds.)
  timeout /t 12 >nul
)
endlocal
exit /b %RC%
