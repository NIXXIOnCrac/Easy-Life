; ============================================================================
;  Easy Life - Windows installer (Inno Setup 6)
;
;  Build the app first (this script installs the FOLDER build, not the
;  single-file one), then compile:
;
;      pyinstaller --noconfirm --clean pcrituals.spec      (portable .exe)
;      set PCRITUALS_TARGET=installed
;      pyinstaller --noconfirm --clean pcrituals.spec      (dist\Easy Life\)
;      ISCC.exe /DAppVersion=0.2.0 installer\pc-rituals.iss
;
;  build_windows.bat does all three steps for you, and so does
;  .github/workflows/release.yml. The user only ever double-clicks the result.
;
;  ----------------------------------------------------------------------------
;  WHY THE APP INSTALLS INTO %LOCALAPPDATA% AND NOT PROGRAM FILES
;
;  The user asked for in-app updating instead of "download a new installer".
;  The in-app updater (pcrituals/update.py, apply()) downloads a .zip and copies
;  the new files straight over the install folder. Windows does not allow an
;  ordinary program to write into C:\Program Files - that needs an
;  administrator prompt every single time. A per-user install under
;  %LOCALAPPDATA% keeps "Update now" working with no UAC prompt at all, and it
;  means installing and uninstalling never need administrator rights either.
;
;  ----------------------------------------------------------------------------
;  WHERE THE USER'S DATA LIVES (and why uninstalling cannot destroy it)
;
;      %LOCALAPPDATA%\Programs\Easy Life   <- the program (this installer, {app})
;      %LOCALAPPDATA%\Easy Life\data       <- rituals, settings, pairings, log
;
;  The data folder is a SIBLING of the program folder, never a child of it, so
;  "delete the program folder" cannot reach it. Nothing in this script's
;  [UninstallDelete]/[InstallDelete]/[Code] sections ever mentions that path:
;  the only path this installer deletes anything from is {app}. See the
;  CurUninstallStepChanged code at the bottom, which says so to the user's face.
;
;  ----------------------------------------------------------------------------
;  SILENT INSTALL (used by setup_windows.bat's wizard and by anyone scripting
;  a deployment; Inno handles these switches itself):
;
;      Easy-Life-Setup-0.2.0.exe /SILENT /SUPPRESSMSGBOXES /NORESTART
;      Easy-Life-Setup-0.2.0.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
;      Easy-Life-Setup-0.2.0.exe /SILENT /TASKS="desktopicon,firewallrule"
;
;  Without /TASKS a silent install gets the default task set, which is: Start
;  Menu entry yes, desktop shortcut yes, "start at login" no, firewall rule no.
; ============================================================================

; ---------------------------------------------------------------------------
; Version. Deliberately REQUIRED rather than defaulted: a hard-coded fallback
; would silently ship an installer that claims to be an older release, which is
; exactly the kind of thing nobody notices until users are confused. Both
; build_windows.bat and the release workflow read the version out of
; pcrituals/__init__.py and pass it in.
; ---------------------------------------------------------------------------
#ifndef AppVersion
  #error AppVersion is not defined. Compile like this:  ISCC.exe /DAppVersion=0.2.0 installer\pc-rituals.iss   (build_windows.bat does this for you)
#endif

; Refuse to build an installer around a bundle that does not exist, instead of
; letting Inno emit a cryptic "Source file does not exist" error.
#if !FileExists(AddBackslash(SourcePath) + '..\dist\Easy Life\Easy Life.exe')
  #error The app folder was not built. Run build_windows.bat (or, from the project root: set PCRITUALS_TARGET=installed ^& pyinstaller --noconfirm --clean pcrituals.spec) and then compile this script again.
#endif

#define AppName        "Easy Life"
#define AppPublisher   "Easy Life"
#define AppExeName     "Easy Life.exe"
; Change these two if you host the project somewhere else - they are only used
; for the "support" link and the update-feed example in the docs.
#define AppUrl         "https://github.com/OWNER/pc-rituals"

[Setup]
; A stable AppId means an upgrade replaces the old version instead of installing
; a second copy beside it. Never change it once a release is out.
AppId={{7C4F1E62-9B3A-4E7D-9A21-5F0C2D8B4A11}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppSupportURL={#AppUrl}
VersionInfoVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableWelcomePage=no
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=Easy-Life-Setup-{#AppVersion}
SetupIconFile=..\pcrituals\web\icons\pcrituals.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; "x64" (rather than the newer "x64compatible") so this compiles on any Inno
; Setup 6.x the user happens to have installed.
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
; The app must not be running while its files are replaced: ask Windows to close
; it (and offer to reopen it afterwards) instead of failing with "file in use".
CloseApplications=yes
CloseApplicationsFilter={#AppExeName}
RestartApplications=no
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
; Start Menu entry is not optional - it is created unconditionally in [Icons].
; checkedonce: on an upgrade, remember the choice the user made the first time
; rather than helpfully putting the icon back they deleted.
Name: "desktopicon";   Description: "Create a &desktop shortcut";                            Flags: checkedonce
Name: "startupicon";   Description: "Start {#AppName} automatically when I log in";           Flags: unchecked
Name: "firewallrule";  Description: "Let my iPhone control this PC (adds a Windows Firewall rule for this PC only)"; Flags: unchecked

[Files]
; The folder build (dist\Easy Life\) - exe + _internal\. ignoreversion so a
; rebuild always overwrites; recursesubdirs keeps the bundle layout intact.
Source: "..\dist\Easy Life\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Helper the firewall task runs. It is small, plain-text and self-elevating.
Source: "..\scripts\add_firewall_rule.bat"; DestDir: "{app}\scripts"; Flags: ignoreversion

[InstallDelete]
; A clean _internal\ on every install guarantees no stale files from an older
; version survive an upgrade and get mixed with the new ones. {app} is the
; program folder only - the user's data folder is not inside it.
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Comment: "Run your Easy Life"
Name: "{autodesktop}\{#AppName}";  Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Comment: "Run your Easy Life"; Tasks: desktopicon

[Registry]
; "Run at login" lives in HKCU (per-user) and uninsdeletevalue makes the
; uninstaller remove it again. No administrator rights needed.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#AppName}"; ValueData: """{app}\{#AppExeName}"""; Tasks: startupicon; Flags: uninsdeletevalue

[Run]
; Optional: open the port the phone app talks to. The helper asks Windows for
; administrator rights itself (one prompt) so the rest of the install stays
; unprivileged.
Filename: "{app}\scripts\add_firewall_rule.bat"; Parameters: "quiet"; Flags: runhidden waituntilterminated; Tasks: firewallrule; StatusMsg: "Allowing your iPhone to reach Easy Life..."
Filename: "{app}\{#AppExeName}"; Description: "Run Easy Life now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Best effort: silently fails without administrator rights, which is acceptable
; (an inbound rule left behind is harmless and the helper can be re-run).
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Easy Life"""; Flags: runhidden; RunOnceId: "PCRitualsFirewallRule"

[UninstallDelete]
; Intentionally EMPTY. Nothing the uninstaller removes lives outside {app}, and
; the user's rituals are outside {app}. Files that arrived through an in-app
; update (which Inno's own log does not know about) are cleaned up in
; CurUninstallStepChanged below - by path, under {app} only.

[Code]
const
  CEdgeClientKey = 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

{ True when the Microsoft Edge WebView2 runtime is present. That runtime draws
  the app's own window; without it Easy Life still runs but opens in the normal
  browser instead, so this is only ever a friendly notice, never a blocker. }
function WebView2Installed: Boolean;
var
  Pv: String;
begin
  Result := RegQueryStringValue(HKCU, CEdgeClientKey, 'pv', Pv)
         or RegQueryStringValue(HKLM, CEdgeClientKey, 'pv', Pv)
         or RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Pv);
end;

{ After the files are in place: tell the user where their rituals live, and
  mention WebView2 if it is missing. SuppressibleMsgBox appears as a plain
  message normally and is skipped in /SILENT installs, so the silent path never
  waits for an answer. }
procedure CurStepChanged(CurStep: TSetupStep);
var
  DataDir: String;
  Extra: String;
begin
  if CurStep <> ssPostInstall then
    Exit;
  DataDir := ExpandConstant('{localappdata}\Easy Life\data');
  Extra := '';
  if not WebView2Installed then
    Extra := #13#10 + #13#10 +
      'Note: this PC has no Microsoft Edge WebView2 runtime yet, so Easy Life ' +
      'will open in your normal browser. Installing the free "WebView2 Runtime" ' +
      'from Microsoft gives you the proper app window.';
  SuppressibleMsgBox(
    'Easy Life is installed.' + #13#10 + #13#10 +
    'Your rituals, settings and paired phones are kept in:' + #13#10 +
    DataDir + #13#10 +
    'Uninstalling Easy Life never deletes that folder.' + Extra,
    mbInformation, MB_OK, MB_OK);
end;

{ Before the program folder is removed: delete the leftovers an in-app update
  may have added (Inno's own uninstall log only knows the files it installed),
  then tell the user their data was deliberately kept.

  NOTHING HERE TOUCHES THE DATA FOLDER. The only paths deleted are {app}\_internal
  and {app}\Easy Life.exe, and the data folder is a sibling of {app}. }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep <> usUninstall then
    Exit;

  DelTree(ExpandConstant('{app}\_internal'), True, True, True);
  DeleteFile(ExpandConstant('{app}\{#AppExeName}'));
  RemoveDir(ExpandConstant('{app}'));

  DataDir := ExpandConstant('{localappdata}\Easy Life\data');
  if DirExists(DataDir) and not UninstallSilent then
    SuppressibleMsgBox(
      'Easy Life has been uninstalled.' + #13#10 + #13#10 +
      'Your rituals and settings were KEPT in:' + #13#10 +
      DataDir + #13#10 + #13#10 +
      'Delete that folder yourself if you really want them gone. ' +
      'Installing Easy Life again will pick them straight back up.',
      mbInformation, MB_OK, MB_OK);
end;
