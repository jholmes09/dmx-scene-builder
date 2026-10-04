; Inno Setup script: wraps the PyInstaller one-folder build into "DMX Scene Builder Setup.exe".
; Build:  iscc /DAppVersion=1.0.0 packaging\installer.iss     (run from the repo root, after pyinstaller)
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "DMX Scene Builder"

[Setup]
AppId={{8F3C5B1E-4D2A-4B7E-9E61-3D5C0E1EB001}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Jeff Holmes Presents
; Per-user install: no administrator password needed. Goes in %LOCALAPPDATA%\Programs.
PrivilegesRequired=lowest
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=DMX Scene Builder Setup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#AppName}.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Messages]
FinishedLabel=DMX Scene Builder is installed.%n%nThe first time it starts, Windows will ask whether to allow it on your network. Tick "Private networks" and click "Allow access". That is what lets an iPad and your lighting boxes reach it.%n%nYour floats are kept when you update or uninstall.

[Tasks]
Name: "desktopicon"; Description: "Put a shortcut on the Desktop"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\DMX Scene Builder\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppName}.exe"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppName}.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppName}.exe"; Description: "Start {#AppName} now"; Flags: nowait postinstall skipifsilent
