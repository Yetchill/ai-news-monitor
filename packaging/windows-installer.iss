; Build with scripts/package.ps1. MyAppVersion is read from pyproject.toml;
; do not hard-code a version in this file.
#ifndef MyAppVersion
  #error MyAppVersion must be passed by scripts/package.ps1
#endif
#ifndef SourceDir
  #error SourceDir must point to the PyInstaller onedir output
#endif

#define MyAppName "AI 情报助手"
#define MyAppPublisher "Yetchill"
#define MyAppExeName "AI 情报助手.exe"

[Setup]
AppId={{B1B8E4C0-6A24-4D27-9D90-3A9E5A1E6F51}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\AI Intelligence Monitor
DefaultGroupName=AI 情报助手
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\artifacts
OutputBaseFilename=AI-Intelligence-Monitor-Setup-{#MyAppVersion}-x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
CloseApplications=yes
CloseApplicationsFilter={#MyAppExeName}
RestartApplications=no

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

; Mutable data lives in {localappdata}\AIIntelligenceMonitor, not under {app}.
; Uninstall intentionally leaves that user data untouched.
