; yhShell per-user Windows x64 installer. Requires Inno Setup 6.3 or newer.
; Compile from any working directory; defaults are relative to this script.
; ISCC /DAppVersion=1.2.3 /DSourceDir="D:\build\yhShell" /DOutputDir="D:\out" packaging\yhShell.iss
; MyAppVersion/MySourceDir/MyOutputDir are accepted for the build.ps1 wrapper.
#ifndef AppVersion
  #ifdef MyAppVersion
    #define AppVersion MyAppVersion
  #else
    #define AppVersion "0.1.0"
  #endif
#endif
#ifndef SourceDir
  #ifdef MySourceDir
    #define SourceDir MySourceDir
  #else
    #define SourceDir SourcePath + "..\dist\yhShell"
  #endif
#endif
#ifndef OutputDir
  #ifdef MyOutputDir
    #define OutputDir MyOutputDir
  #else
    #define OutputDir SourcePath + "..\dist"
  #endif
#endif

[Setup]
; Keep this GUID stable across all versions so upgrades use the same installation.
AppId={{CC940730-F218-4698-B2E9-C22034804794}
AppName=yhShell
AppVersion={#AppVersion}
AppPublisher=5YHao
AppPublisherURL=https://github.com/5YHao/yhShell
AppSupportURL=https://github.com/5YHao/yhShell/issues
AppUpdatesURL=https://github.com/5YHao/yhShell/releases
DefaultDirName={localappdata}\Programs\yhShell
DefaultGroupName=yhShell
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=yhShell-Setup-{#AppVersion}-windows-x64
UninstallDisplayName=yhShell
UninstallDisplayIcon={app}\yhShell.exe
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
SetupLogging=yes
CloseApplications=yes
RestartApplications=no
; Signing is optional release infrastructure, not a requirement to build/run.
; Never add {userappdata}\yhShell to [InstallDelete] or [UninstallDelete].

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
; Simplified Chinese is not bundled by every Inno Setup installation.
#if FileExists(AddBackslash(CompilerPath) + "Languages\ChineseSimplified.isl")
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
#endif

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
; Preserve the complete PyInstaller onedir tree, including _internal and assets.
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\yhShell"; Filename: "{app}\yhShell.exe"; WorkingDir: "{app}"
Name: "{group}\Uninstall yhShell"; Filename: "{uninstallexe}"
Name: "{autodesktop}\yhShell"; Filename: "{app}\yhShell.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\yhShell.exe"; Description: "Launch yhShell"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent; Check: HasWebView2

[Code]
const
  // Microsoft-documented Evergreen Runtime GUID, not the Edge browser GUID:
  // https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution
  WebView2Key = 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WebView2DownloadURL = 'https://developer.microsoft.com/microsoft-edge/webview2/consumer/';

function HasRuntimeInRegistry(RootKey: Integer): Boolean;
var
  Value: String;
  Version: Int64;
begin
  Result := False;
  if RegQueryStringValue(RootKey, WebView2Key, 'pv', Value) then
    if StrToVersion(Trim(Value), Version) then
      Result := Version > 0;
end;

function HasWebView2: Boolean;
begin
  // HKCU's documented per-user key and the 32-bit HKLM view on 64-bit Windows.
  // HKLM32 maps to SOFTWARE\WOW6432Node; do not duplicate that segment.
  Result := HasRuntimeInRegistry(HKCU) or HasRuntimeInRegistry(HKLM32);
end;

procedure OpenWebView2Download(Sender: TObject);
var
  ErrorCode: Integer;
begin
  // Only open the official web page after an explicit user click.
  // This installer never downloads, bundles, or executes a runtime installer.
  if not ShellExec('open', WebView2DownloadURL, '', '', SW_SHOWNORMAL,
    ewNoWait, ErrorCode) then
    MsgBox('Could not open the browser. Visit:' + #13#10 +
      WebView2DownloadURL, mbInformation, MB_OK);
end;

procedure InitializeWizard;
var
  Page: TOutputMsgWizardPage;
  DownloadButton: TNewButton;
  DependencyText: String;
begin
  DependencyText :=
    'yhShell requires Windows 10 or Windows 11 (x64-compatible), ' +
    'Microsoft .NET Framework 4.8 or later, and the Microsoft Edge WebView2 ' +
    'Evergreen Runtime.' + #13#10 + #13#10 +
    '.NET Framework 4.8 is included in newer Windows 10 releases and Windows 11. ' +
    'On older Windows 10 installations, install it using Windows Update or ' +
    'Microsoft''s official .NET Framework download page before running yhShell.' + #13#10 + #13#10;

  if HasWebView2 then
    DependencyText := DependencyText + 'WebView2 Runtime was detected.'
  else begin
    DependencyText := DependencyText +
      'WARNING: WebView2 Runtime was not detected. yhShell needs it to display ' +
      'its interface. Use the button below to open Microsoft''s official download ' +
      'page and install the Evergreen Runtime yourself before launching yhShell. ' +
      'Setup can continue without it (including offline installation). ' +
      'No runtime installer is bundled, downloaded, or executed by this setup.';
    Log('WARNING: WebView2 Runtime was not detected. Install the Evergreen Runtime from ' +
      WebView2DownloadURL + ' before launching yhShell.');
  end;

  Page := CreateOutputMsgPage(wpWelcome, 'Runtime requirements',
    'Check prerequisites before installing yhShell', DependencyText);
  Page.MsgLabel.Height := Page.SurfaceHeight - ScaleY(42);

  DownloadButton := TNewButton.Create(Page);
  DownloadButton.Parent := Page.Surface;
  DownloadButton.Left := 0;
  DownloadButton.Top := Page.SurfaceHeight - ScaleY(30);
  DownloadButton.Width := ScaleX(250);
  DownloadButton.Height := ScaleY(25);
  DownloadButton.Caption := 'Open official WebView2 download page';
  DownloadButton.Enabled := not HasWebView2;
  DownloadButton.OnClick := @OpenWebView2Download;
end;

// User settings, credentials, keys and history under APPDATA are retained.
