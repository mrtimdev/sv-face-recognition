; Inno Setup script for SV Face ID (Windows installer)
;
; Download Inno Setup: https://jrsoftware.org/isinfo.php
; Compile: iscc scripts\installer.iss

[Setup]
AppName=SV Face ID
AppVersion=1.0.0
AppPublisher=SV Technologies
DefaultDirName={autopf}\SV Face ID
DefaultGroupName=SV Face ID
OutputDir=dist
OutputBaseFilename=SV-Face-ID-Setup-1.0.0
Compression=lzma2
SolidCompression=yes
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\SV Face ID.exe
WizardStyle=modern
PrivilegesRequired=lowest

[Files]
Source: "dist\SV Face ID\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs

[Icons]
Name: "{group}\SV Face ID"; Filename: "{app}\SV Face ID.exe"
Name: "{autodesktop}\SV Face ID"; Filename: "{app}\SV Face ID.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"

[Run]
Filename: "{app}\SV Face ID.exe"; Description: "Launch SV Face ID"; Flags: nowait postinstall skipifsilent
