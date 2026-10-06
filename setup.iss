; ShredPack installer script for Inno Setup (https://jrsoftware.org/isinfo.php)
;
; Build order (e.g. in your GitHub Action):
;   1. pyinstaller --noconsole --onefile --collect-all py7zr --collect-all pycdlib
;        --collect-all tkinterdnd2 --name ShredPack shredpack.py
;      -> produces dist\ShredPack.exe
;   2. "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" setup.iss
;      -> produces Output\ShredPack-Setup.exe
;
; This script does NOT register the right-click menu itself. It calls
; "ShredPack.exe --install" after copying files, and "--uninstall" before
; removing them, so the registry always points at the fixed install folder
; below and never breaks if the user moves anything.

#define MyAppName "ShredPack"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "ShredPack"
#define MyAppExeName "ShredPack.exe"

[Setup]
AppId={{8F2B6E2B-5B0B-4C2E-9B0E-5F1F6B8D9A11}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
SetupIconFile=shredpack.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
OutputBaseFilename=ShredPack-Setup
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequiredOverridesAllowed=dialog
; ShredPack writes only to HKCU, so a per-user install needs no elevation.
; "dialog" lets the user pick Admin (Program Files, all users) or User
; (AppData, just themselves) at install time, same choice WinRAR offers.
PrivilegesRequired=lowest
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked
Name: "contextmenu"; Description: "Add ""Extract with ShredPack"" to the right-click menu"; GroupDescription: "Windows integration:"; Flags: checkedonce

[Files]
Source: "dist\ShredPack.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; Register the right-click menu once files are in their final, permanent location.
Filename: "{app}\{#MyAppExeName}"; Parameters: "--install"; Flags: runhidden waituntilterminated; Tasks: contextmenu
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent unchecked

[UninstallRun]
; Clean up the registry before the files themselves are removed.
Filename: "{app}\{#MyAppExeName}"; Parameters: "--uninstall"; Flags: runhidden waituntilterminated

[Code]
// If a previous "unpackaged" copy of ShredPack registered its own right-click
// menu from some other folder, this finds and clears it so the installed
// copy doesn't collide with a stale entry. Safe to run even if none exists.
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    // Nothing extra needed: ShredPack.exe --install always overwrites any
    // previous registry entries with ones pointing at {app}.
  end;
end;
