; 荣珂 · 朋友版安装器（Inno Setup 6）
;
; 与正式版 `rongke.iss` 的区别，只有三处：
;   1. 多打进两份**预置材料**（preset/config.json + preset/secrets.json）；
;   2. 装完把它们写进新用户的 ~/.rongke（**仅在文件不存在时**写，不覆盖别人的配置）；
;   3. 输出名带 -Friend，AppId 与正式版不同，避免覆盖你自己机器上已装的荣珂。
;
; ⚠️ 安全边界：preset/ 里含**明文 API 密钥**，任何拿到这个 exe 的人都能取用。
;    _friend_private/ 必须留在 .gitignore 里，绝不进版本库 / wheel / sdist /
;    正式版安装包。正式版共用同一个 StageDir，因此 stage 里绝不放 preset。

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef StageDir
  #define StageDir "..\dist\Rongke"
#endif

[Setup]
; ★ 与正式版不同的 AppId：这样在本机做验证安装时不会顶掉正式版的卸载登记。
AppId={{B4F2A6C1-3D7E-4A58-9C2B-1E6F8D0A7B94}
AppName=荣珂
AppVersion={#AppVersion}
AppVerName=荣珂 {#AppVersion}（朋友版）
AppPublisher=荣珂
AppPublisherURL=https://github.com/Yrzsh/rongke
AppSupportURL=https://github.com/Yrzsh/rongke/issues
AppUpdatesURL=https://github.com/Yrzsh/rongke/releases
DefaultDirName={localappdata}\Programs\Rongke
DefaultGroupName=荣珂
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=RongkeSetup-{#AppVersion}-Friend
SetupIconFile=..\rongke\web\app.ico
UninstallDisplayIcon={app}\Rongke.exe
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
LicenseFile=..\LICENSE
InfoAfterFile=FRIEND-NOTES.txt
VersionInfoVersion={#AppVersion}.0
VersionInfoCompany=荣珂
VersionInfoDescription=荣珂 安装程序（朋友版 · 已预置 API）
VersionInfoProductName=荣珂
VersionInfoProductVersion={#AppVersion}
CloseApplications=yes

#ifdef UseChinese
[Languages]
Name: "chs"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
#else
[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
#endif

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: checkedonce

[Files]
Source: "{#StageDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
; 双轨许可：装完的人必须能在本机同时看到两份许可。
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE-DESKTOP.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\NOTICE.md"; DestDir: "{app}"; Flags: ignoreversion
; ★ 朋友版专属：预置网关 + 密钥。源目录不进版本库。
Source: "_friend_private\config.json"; DestDir: "{app}\preset"; Flags: ignoreversion
Source: "_friend_private\secrets.json"; DestDir: "{app}\preset"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\荣珂"; Filename: "{app}\Rongke.exe"; Parameters: "--port 8788"; WorkingDir: "{app}"; Comment: "荣珂 · 科研 agent 控制台"
Name: "{autodesktop}\荣珂"; Filename: "{app}\Rongke.exe"; Parameters: "--port 8788"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\Rongke.exe"; Parameters: "--port 8788"; Description: "启动 荣珂"; Flags: nowait postinstall skipifsilent

[Code]
// 安装结束后把预置材料落到新用户的家目录。
// 「配置在场 ≠ 已生效」：config.json 的读取顺序是
//   内置默认 → ~/.rongke/config.json → 项目内 config.json → 环境变量，
// 所以必须写进 ~/.rongke，写在 {app} 里内核根本看不到。
// 只写不存在的文件：朋友若已经自己配过 API，绝不覆盖他的东西。
procedure CurStepChanged(CurStep: TSetupStep);
var
  DataDir, SrcSec, DstSec, SrcCfg, DstCfg: string;
begin
  if CurStep <> ssPostInstall then Exit;

  DataDir := ExpandConstant('{%USERPROFILE}') + '\.rongke';
  if not ForceDirectories(DataDir) then
  begin
    MsgBox('无法创建数据目录：' + DataDir, mbError, MB_OK);
    Exit;
  end;

  SrcCfg := ExpandConstant('{app}\preset\config.json');
  DstCfg := DataDir + '\config.json';
  if not FileExists(DstCfg) then
  begin
    if not FileCopy(SrcCfg, DstCfg, False) then
      MsgBox('预置网关配置写入失败：' + DstCfg, mbError, MB_OK);
  end;

  SrcSec := ExpandConstant('{app}\preset\secrets.json');
  DstSec := DataDir + '\secrets.json';
  if not FileExists(DstSec) then
  begin
    if not FileCopy(SrcSec, DstSec, False) then
      MsgBox('预置 API 密钥写入失败：' + DstSec, mbError, MB_OK);
  end;
end;
