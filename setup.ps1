#Requires -Version 5.1
# yhShell setup: create a 64-bit virtualenv and install dependencies.
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

function Test-Python64 {
    param([string]$Exe, [string[]]$Pre = @())
    try {
        $out = & $Exe @Pre -c "import sys,platform;print(platform.architecture()[0], sys.version_info[0], sys.version_info[1])" 2>$null
        if ($LASTEXITCODE -ne 0) { return $false }
        $parts = (($out -join ' ').Trim() -split '\s+')
        if ($parts.Count -lt 3) { return $false }
        return ($parts[0] -eq '64bit' -and [int]$parts[1] -ge 3 -and [int]$parts[2] -ge 10)
    } catch {
        return $false
    }
}

$python = $null
$preArgs = @()

if ($env:YHSELL_PYTHON -and (Test-Path $env:YHSELL_PYTHON) -and (Test-Python64 $env:YHSELL_PYTHON)) {
    $python = $env:YHSELL_PYTHON
}

if (-not $python -and (Get-Command py -ErrorAction SilentlyContinue)) {
    if (Test-Python64 'py' @('-3')) {
        $python = 'py'
        $preArgs = @('-3')
    }
}

$candidates = @(
    (Join-Path $env:USERPROFILE '.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe'),
    'C:\Program Files\Python312\python.exe',
    'C:\Program Files\Python311\python.exe',
    'C:\Program Files\Python313\python.exe',
    'C:\Python312\python.exe',
    (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe')
)

if (-not $python) {
    foreach ($candidate in $candidates) {
        if ((Test-Path $candidate) -and (Test-Python64 $candidate)) {
            $python = $candidate
            break
        }
    }
}

if (-not $python -and (Get-Command python -ErrorAction SilentlyContinue)) {
    if (Test-Python64 'python') { $python = 'python' }
}

if (-not $python) {
    Write-Host ''
    Write-Host 'No 64-bit Python found.' -ForegroundColor Red
    Write-Host 'Install 64-bit Python 3.10+ from https://www.python.org/downloads/windows/'
    Write-Host 'then run this script again (or set YHSELL_PYTHON to python.exe).'
    exit 1
}

Write-Host "Using Python: $python $($preArgs -join ' ')"
if (-not (Test-Path '.venv\Scripts\python.exe')) {
    & $python @preArgs -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'venv creation failed' }
}

& '.venv\Scripts\python.exe' -m pip install --upgrade pip
& '.venv\Scripts\python.exe' -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'dependency installation failed' }

Write-Host ''
Write-Host 'Done. Start the app with run.bat' -ForegroundColor Green
