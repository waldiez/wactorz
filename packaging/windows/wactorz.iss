; Inno Setup script for the Wactorz desktop app (Windows).
;
; Build: run packaging/windows/build-windows.ps1 (PyInstaller -> this), or:
;   iscc /DMyAppVersion=<x.y.z> packaging\windows\wactorz.iss
; Expects the PyInstaller onedir output at dist\wactorz-desktop\ .

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#define MyAppName       "Wactorz"
#define MyAppPublisher  "Wactorz Contributors"
#define MyAppURL        "https://github.com/waldiez/wactorz"
#define MyAppExeName    "wactorz-desktop.exe"
#define MyAppModelId    "io.waldiez.wactorz"

[Setup]
; Keep AppId constant across versions (controls upgrades/uninstall).
AppId={{B1F4A2C0-9E3D-4C7A-AC21-7B6E2F1D9A30}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
; Show just "Wactorz" in Installed apps (version stays in its own column),
; instead of the default "Wactorz version x.y.z".
UninstallDisplayName={#MyAppName}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
OutputDir=..\..\dist
OutputBaseFilename=Wactorz-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Branding: custom installer icon + wizard images (generated from the app icon).
SetupIconFile=..\..\wactorz\desktop\assets\icon.ico
WizardImageFile=wizard-large.bmp
WizardSmallImageFile=wizard-small.bmp
; Per-user install by default; user may switch to all-users in the wizard.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
Source: "..\..\dist\wactorz-desktop\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
; To auto-install the WebView2 runtime, drop MicrosoftEdgeWebview2Setup.exe next
; to this script and uncomment the next line (and the matching [Run] entry):
; Source: "MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: not WebView2Installed

[Icons]
# Flat {autoprograms} entry (no program-group subfolder) and no shortcut
# AppUserModelID — both can hide the app from the Windows 11 "All apps" list.
# Taskbar grouping still works: the app sets its AUMID at runtime (see
# _set_app_user_model_id). Uninstall is via Settings → Apps (AppId registers it).
Name: "{autoprograms}\{#MyAppName}";  Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}";   Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "Installing the WebView2 runtime..."; Check: not WebView2Installed
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
{ True if the Evergreen WebView2 Runtime is present (per-machine or per-user). }
function WebView2Installed(): Boolean;
var
  pv: String;
begin
  Result :=
    RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', pv) or
    RegQueryStringValue(HKCU, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', pv);
end;

{ On uninstall, offer to remove the per-user data dir (logs, DB, state). Kept by
  default so a reinstall preserves data; only the uninstalling user's dir. }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  dataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    dataDir := ExpandConstant('{localappdata}\wactorz');
    if DirExists(dataDir) then
      if MsgBox('Also delete Wactorz data (logs, database, settings) at'#13#10 +
                dataDir + ' ?', mbConfirmation, MB_YESNO) = IDYES then
        DelTree(dataDir, True, True, True);
  end;
end;
