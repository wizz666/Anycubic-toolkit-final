; Inno Setup script for Anycubic Toolkit.
;
; Builds a proper Windows installer (Start Menu shortcut, optional desktop
; icon, uninstaller registered in "Add or Remove Programs") around the
; PyInstaller-built dist\AnycubicToolkit.exe.
;
; Requires the app to already be built first:
;   pyinstaller anycubic_toolkit.spec
; Then compile this script (from a normal command prompt, not needed from
; Claude Code):
;   "C:\Users\<you>\AppData\Local\Programs\Inno Setup 6\ISCC.exe" installer\anycubic_toolkit.iss
;
; AppVersion below is kept in sync by hand with src/anycubic_toolkit/__init__.py's
; __version__ - bump both together on release.

#define AppName "Anycubic Toolkit"
#define AppVersion "0.3.2"
#define AppPublisher "Wizz"
#define AppURL "https://github.com/wizz666/Anycubic-toolkit-final"
#define AppExeName "AnycubicToolkit.exe"

[Setup]
; Unique per-app identifier (do not change between versions - it's how
; Windows recognizes "this is an upgrade of the same app" vs a new install).
AppId={{1B93E4B9-D4F0-4334-BB7F-7708ADFEEBC5}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
; Per-user install under the user's own AppData - no admin/UAC prompt needed,
; matches how most small free hobby apps (VS Code, Discord, ...) install.
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=output
OutputBaseFilename=AnycubicToolkit-Setup-{#AppVersion}
SetupIconFile=..\src\anycubic_toolkit\resources\icons\app.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
LicenseFile=..\LICENSE

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\AnycubicToolkit.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
