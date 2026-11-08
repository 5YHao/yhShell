#Requires -Version 5.1
# ASCII source intentionally supports Windows PowerShell 5.1 without a UTF-8 BOM.
[CmdletBinding()]
param(
    [switch]$Clean,
    [switch]$SkipInstaller,
    [string]$Version = '',
    [string]$IsccPath = ''
)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) { throw 'Run setup.ps1 first: .venv Python is missing.' }
& $Python -c "import struct,platform; assert struct.calcsize('P')==8 and platform.machine().upper() in ('AMD64','X86_64')"
if ($LASTEXITCODE -ne 0) { throw '64-bit x64 Python is required.' }
$actualVersion = (& $Python -c 'from yhshell import __version__; print(__version__)').Trim()
if ($LASTEXITCODE -ne 0 -or $actualVersion -notmatch '^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$') { throw "Invalid stable X.Y.Z app version: $actualVersion" }
if ($Version -and $Version.TrimStart('v') -ne $actualVersion) { throw "Version $Version differs from code version $actualVersion" }
$Version = $actualVersion
$Dist = Join-Path $Root 'dist\yhShell'
$DistRoot = Join-Path $Root 'dist'
$Zip = Join-Path $DistRoot "yhShell-$Version-windows-x64.zip"
$Sha = Join-Path $DistRoot 'SHA256SUMS.txt'
$Spec = Join-Path $Root 'packaging\yhShell.spec'
$PyInstaller = Join-Path $Root '.venv\Scripts\pyinstaller.exe'
if (-not (Test-Path $PyInstaller)) {
    & $Python -m pip install 'pyinstaller==6.22.3'
    if ($LASTEXITCODE -ne 0) { throw 'Failed to install PyInstaller.' }
}
# Only the fixed workspace build output may be removed.
if ($Clean -and (Test-Path $DistRoot)) {
    $resolved = (Resolve-Path -LiteralPath $DistRoot).Path
    if ($resolved -ne "$Root\dist") { throw "Unexpected cleanup target: $resolved" }
    Write-Host "Removing verified output directory: $resolved"
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $DistRoot | Out-Null
& $PyInstaller '--noconfirm' '--clean' $Spec
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
if (-not (Test-Path (Join-Path $Dist 'yhShell.exe'))) { throw 'Missing yhShell.exe.' }
# Copy public release instructions into both distributions; never copy user data.
Copy-Item -LiteralPath (Join-Path $Root 'packaging\PORTABLE.txt') -Destination (Join-Path $Dist 'README.txt') -Force
if (Test-Path $Zip) {
    Write-Host "Replacing verified archive: $Zip"
    if ((Split-Path $Zip -Parent) -ne $DistRoot) { throw 'Unexpected ZIP output path.' }
    Remove-Item -LiteralPath $Zip -Force
}
Compress-Archive -Path (Join-Path $Dist '*') -DestinationPath $Zip -CompressionLevel Optimal
$hash = (Get-FileHash -LiteralPath $Zip -Algorithm SHA256).Hash.ToLowerInvariant()
"$hash  $(Split-Path $Zip -Leaf)" | Set-Content -LiteralPath $Sha -Encoding ASCII
$installer = $null
if (-not $SkipInstaller) {
    $compiler = @(
        $IsccPath,
        (Get-Command ISCC.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
        (Join-Path $Root '.tools\InnoSetup\ISCC.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 7\ISCC.exe'),
        'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
        'C:\Program Files (x86)\Inno Setup 7\ISCC.exe',
        'C:\Program Files\Inno Setup 6\ISCC.exe',
        'C:\Program Files\Inno Setup 7\ISCC.exe'
    ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    if (-not $compiler) { throw 'Portable ZIP built. Install Inno Setup 6.3+ or pass -IsccPath; use -SkipInstaller for ZIP only.' }
    & $compiler "/DMyAppVersion=$Version" "/DMySourceDir=$Dist" "/DMyOutputDir=$DistRoot" (Join-Path $Root 'packaging\yhShell.iss')
    if ($LASTEXITCODE -ne 0) { throw 'Inno Setup build failed.' }
    $installer = Get-Item (Join-Path $DistRoot "yhShell-Setup-$Version-windows-x64.exe")
    $ihash = (Get-FileHash -LiteralPath $installer.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    Add-Content -LiteralPath $Sha -Value "$ihash  $($installer.Name)" -Encoding ASCII
}
Write-Host "Portable ZIP: $Zip"
if ($installer) { Write-Host "Installer: $($installer.FullName)" }
Write-Host "Checksums: $Sha"
