; Inno Setup script for SV Face ID (Windows installer)
;
; Download Inno Setup: https://jrsoftware.org/isinfo.php
; Compile: iscc /DMyAppVersion=1.2.3 scripts\installer.iss
;
; The release workflow passes the version from the Git tag, so the installer is
; named SV-Face-ID-Setup-<version>.exe and Windows lists the right version.
; The in-app updater runs it with /SILENT /RELAUNCH=1 to update in place.

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

[Setup]
; Resolve the icon, packaged files, and output directory from the repository root.
SourceDir=..
; Never change AppId: it is how upgrades find and replace the existing install.
; ("SV Face ID" is also the id Inno Setup derived from AppName for earlier releases.)
AppId=SV Face ID
AppName=SV Face ID
AppVersion={#MyAppVersion}
AppVerName=SV Face ID {#MyAppVersion}
VersionInfoVersion={#MyAppVersion}
AppPublisher=SV Technologies
DefaultDirName={autopf}\SV Face ID
DefaultGroupName=SV Face ID
OutputDir=dist
OutputBaseFilename=SV-Face-ID-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\SV Face ID.exe
WizardStyle=modern
PrivilegesRequired=lowest
; Close a running copy before replacing its files (the updater has usually quit it already).
CloseApplications=yes
RestartApplications=no

[Files]
Source: "dist\SV Face ID\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs

[Icons]
Name: "{group}\SV Face ID"; Filename: "{app}\SV Face ID.exe"
Name: "{autodesktop}\SV Face ID"; Filename: "{app}\SV Face ID.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"

[Run]
; Interactive installs offer "Launch SV Face ID" on the last page.
Filename: "{app}\SV Face ID.exe"; Description: "Launch SV Face ID"; Flags: nowait postinstall skipifsilent
; In-app updates run silently with /RELAUNCH=1 and reopen the app when done.
Filename: "{app}\SV Face ID.exe"; Flags: nowait; Check: RelaunchRequested

[Code]
function RelaunchRequested: Boolean;
begin
  Result := ExpandConstant('{param:relaunch|0}') = '1';
end;
