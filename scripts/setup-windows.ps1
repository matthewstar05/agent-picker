# One-time Windows setup: everything after cloning this repo.
# Installs Python and herdr only when missing, then runs install-windows.ps1.
# Provider CLIs (claude, codex, ...) and their sign-ins are left to you.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-windows.ps1 [-SkipHerdr]
param([switch]$SkipHerdr)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot

function Update-SessionPath {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
}

function Find-Python {
    # The Microsoft Store "python" stub fails this check, so it is skipped.
    $ErrorActionPreference = 'Continue'
    $check = 'import sys; print(sys.executable if sys.version_info >= (3, 9) else "")'
    $exe = $null
    if (Get-Command py -CommandType Application -ErrorAction SilentlyContinue) { $exe = & py -3 -c $check }
    if (-not $exe -and (Get-Command python -CommandType Application -ErrorAction SilentlyContinue)) { $exe = & python -c $check }
    if ($exe -and (Test-Path -LiteralPath "$exe".Trim())) { return "$exe".Trim() }
}

function Find-Herdr {
    $cmd = Get-Command herdr -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $alias = Join-Path $env:LOCALAPPDATA 'Programs\Herdr\bin\herdr.exe'
    if (Test-Path -LiteralPath $alias) { return $alias }
}

function Install-WithWinget([string]$Id) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "winget is unavailable. Install $Id manually, then rerun this script."
    }
    Write-Host "Installing $Id with winget..."
    winget install -e --id $Id --scope user --silent --accept-package-agreements --accept-source-agreements
    Update-SessionPath
}

$python = Find-Python
if (-not $python) {
    Install-WithWinget 'Python.Python.3.13'
    $python = Find-Python
    if (-not $python) { throw 'Python 3.9+ is still not found. Open a new terminal and rerun this script.' }
}
Write-Host "Python: $python"

if (-not $SkipHerdr -and -not (Find-Herdr)) {
    Write-Host 'Installing herdr (https://herdr.dev, Windows beta)...'
    powershell -NoProfile -ExecutionPolicy Bypass -Command 'irm https://herdr.dev/install.ps1 | iex'
    if ($LASTEXITCODE -ne 0) { throw 'herdr install failed. See https://herdr.dev/docs/windows-beta/' }
    Update-SessionPath
}
$herdr = Find-Herdr
if ($herdr) { Write-Host "herdr: $herdr" } elseif (-not $SkipHerdr) { Write-Host 'herdr: installed, available in a new terminal' }

& (Join-Path $PSScriptRoot 'install-windows.ps1') -Python $python

Write-Host ''
Write-Host 'Done. Open a new Windows Terminal tab, then:'
Write-Host '  agent --picker-doctor     check which provider CLIs were found'
Write-Host '  cd <your project>; herdr-ai'
Write-Host 'Providers listed as not installed need their own CLI install and sign-in on this computer.'
