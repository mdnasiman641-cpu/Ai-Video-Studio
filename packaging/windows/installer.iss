; Inno Setup 6 script for AI Video Studio.
; Built by packaging/build.py, which passes AppVersion, SourceDir, OutputDir and IconFile:
;   iscc /DAppVersion=0.3.2 /DSourceDir=dist\AI-Video-Studio /DOutputDir=dist /DIconFile=build\assets\app.ico installer.iss
;
; - Installs per user (no administrator rights) by default; "Install for all users" is offered.
; - Upgrades in place (same AppId); the old program files are replaced, user data is not touched.
; - User data lives outside the program folder (%LOCALAPPDATA%\AIVideoStudio: settings, logs, downloaded
;   models; Documents\AI Video Studio Projects: projects) and is kept on uninstall unless the user
;   explicitly asks to remove it. Projects and videos are never removed.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\AI-Video-Studio"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist"
#endif
#ifndef IconFile
  #define IconFile "..\..\build\assets\app.ico"
#endif
#define AppName "AI Video Studio"
#define AppExe "AI-Video-Studio.exe"
#define ConsoleExe "AI-Video-Studio-Console.exe"

[Setup]
AppId={{8F2B3C1E-6A57-4C0B-9E1D-3B7F2A9C5D41}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=AI Video Studio project
AppPublisherURL=https://github.com/mdnasiman641-cpu/Ai-Video-Studio
AppSupportURL=https://github.com/mdnasiman641-cpu/Ai-Video-Studio/issues
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoDescription={#AppName} Setup
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=AI-Video-Studio-{#AppVersion}-setup-win64
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/normal
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; Remove the previous version's runtime before copying the new one, so no stale libraries remain.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"
Name: "{autoprograms}\{#AppName} Tools\{#AppName} (diagnostic console)"; Filename: "{app}\{#ConsoleExe}"; WorkingDir: "{app}"
Name: "{autoprograms}\{#AppName} Tools\Check {#AppName} environment"; Filename: "{app}\{#ConsoleExe}"; Parameters: "--check-env --pause"; WorkingDir: "{app}"
Name: "{autoprograms}\{#AppName} Tools\{#AppName} self-test"; Filename: "{app}\{#ConsoleExe}"; Parameters: "--self-test --pause"; WorkingDir: "{app}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Runtime files only. A portable-mode "UserData" folder inside {app} is never deleted automatically.
Type: filesandordirs; Name: "{app}\_internal"

[Code]
function UserDataDir(): String;
begin
  Result := ExpandConstant('{localappdata}\AIVideoStudio');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  { Settings, logs and downloaded models are kept unless the user explicitly chooses to remove them.
    Silent uninstalls never remove them. Projects and videos are never removed. }
  if (CurUninstallStep = usPostUninstall) and (not UninstallSilent()) and DirExists(UserDataDir()) then
  begin
    if MsgBox('Also delete your AI Video Studio settings, logs and downloaded AI models?' + #13#10 + #13#10 +
              UserDataDir() + #13#10 + #13#10 +
              'Choose No to keep them for a later reinstall. Your projects and videos are not affected either way.',
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DelTree(UserDataDir(), True, True, True);
  end;
end;
