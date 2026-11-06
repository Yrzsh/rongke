; 荣珂 Windows 安装器（Inno Setup 6）
;
; 设计选择：**按用户安装、不要求管理员**。
;   - 程序进 %LOCALAPPDATA%\Programs\Rongke，不需要 UAC；
;   - 用户数据本来就在 %USERPROFILE%\.rongke，不在 {app} 里；
;   - 卸载只删程序目录，数据保留。
;
; 构建脚本会传 /DAppVersion=... /DStageDir=...；这里给默认值只为让 .iss
; 能单独被 IDE 打开。

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef StageDir
  #define StageDir "..\dist\Rongke"
#endif

[Setup]
AppId={{B4F2A6C1-3D7E-4A58-9C2B-1E6F8D0A7B93}
AppName=荣珂
AppVersion={#AppVersion}
AppVerName=荣珂 {#AppVersion}
AppPublisher=荣珂
AppPublisherURL=https://github.com/Yrzsh/rongke
AppSupportURL=https://github.com/Yrzsh/rongke/issues
AppUpdatesURL=https://github.com/Yrzsh/rongke/releases
DefaultDirName={localappdata}\Programs\Rongke
DefaultGroupName=荣珂
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=RongkeSetup-{#AppVersion}
SetupIconFile=..\rongke\web\app.ico
UninstallDisplayIcon={app}\Rongke.exe
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
LicenseFile=..\LICENSE
InfoAfterFile=INSTALL-NOTES.txt
VersionInfoVersion={#AppVersion}.0
VersionInfoCompany=荣珂
VersionInfoDescription=荣珂 安装程序
VersionInfoProductName=荣珂
VersionInfoProductVersion={#AppVersion}
CloseApplications=yes

; 中文语言文件不是 Inno Setup 官方安装包必带的；构建脚本先探测，
; 有才传 /DUseChinese=1，没有就退回英文，避免硬写一个不存在的路径。
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
; ★ 2026-11-05 双轨许可：安装完的人必须能在本机读到**两份**许可，
;   不能只看到框架的 Apache-2.0 就以为 exe 也开源。
;   Source 相对路径按 .iss 所在目录（packaging/）解析，与调用 cwd 无关。
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE-DESKTOP.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\NOTICE.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; ★ 必须显式带 --port 8788：exe 的默认端口是 8765，而 8765 在不少机器上
;   被别的东西占着；不带参数的快捷方式双击后会「拒绝启动」而不是换端口。
Name: "{autoprograms}\荣珂"; Filename: "{app}\Rongke.exe"; Parameters: "--port 8788"; WorkingDir: "{app}"; Comment: "荣珂 · 科研 agent 控制台"
Name: "{autodesktop}\荣珂"; Filename: "{app}\Rongke.exe"; Parameters: "--port 8788"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\Rongke.exe"; Parameters: "--port 8788"; Description: "启动 荣珂"; Flags: nowait postinstall skipifsilent
