# Installs agent-picker together with its own Herdr:
#
#   irm https://github.com/matthewstar05/agent-picker/releases/latest/download/install.ps1 | iex
#
# The release bundle goes into %LOCALAPPDATA%\agent-picker\app (AGENT_PICKER_HOME
# changes that), its commands into %LOCALAPPDATA%\agent-picker\bin. A Herdr you
# already have is left alone. Running this again updates the install.
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$repo = "matthewstar05/agent-picker"
$releases = if ($env:AGENT_PICKER_RELEASES_URL) { $env:AGENT_PICKER_RELEASES_URL } else { "https://github.com/$repo/releases/latest/download" }
$local = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { Join-Path $HOME "AppData\Local" }
$dest = if ($env:AGENT_PICKER_HOME) { $env:AGENT_PICKER_HOME } else { Join-Path $local "agent-picker\app" }

function Fail($message) { throw "agent-picker: $message" }

function Fetch($url, $path) {
    if ($url -like "file:*") { Copy-Item -LiteralPath ([Uri]$url).LocalPath -Destination $path -Force }
    else { Invoke-WebRequest -Uri $url -OutFile $path -UseBasicParsing }
}

if ([Environment]::Is64BitOperatingSystem -eq $false) { Fail "needs 64-bit Windows" }
$target = "windows-x86_64"

$python = $null
$candidates = @()
if ($env:AGENT_PICKER_PYTHON) { $candidates += ,@($env:AGENT_PICKER_PYTHON) }
$candidates += ,@("py", "-3"), @("python"), @("python3")
foreach ($candidate in $candidates) {
    $exe = $candidate[0]
    $rest = @($candidate | Select-Object -Skip 1)
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    try {
        & $exe @rest -c "import sys; raise SystemExit(sys.version_info < (3, 9))" 2>$null
        if ($LASTEXITCODE -eq 0) { $python = $candidate; break }
    } catch { }
}
if (-not $python) { Fail "needs Python 3.9 or later from python.org" }

if (Test-Path (Join-Path $dest ".git")) { Fail "$dest is a Git checkout; update it with: agent --picker-update" }

$tmp = Join-Path ([IO.Path]::GetTempPath()) ("agent-picker-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
    $name = "agent-picker-$target.zip"
    Write-Host "Downloading agent-picker for $target..."
    $sums = Join-Path $tmp "sums.txt"
    Fetch "$releases/SHA256SUMS.txt" $sums
    $want = $null
    foreach ($line in Get-Content $sums) {
        $parts = $line.Trim() -split "\s+", 2
        if ($parts.Count -eq 2 -and $parts[1].TrimStart("*") -eq $name) { $want = $parts[0].ToLower() }
    }
    if (-not $want) { Fail "this release has no build for $target" }
    $archive = Join-Path $tmp $name
    Fetch "$releases/$name" $archive
    $have = (Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash.ToLower()
    if ($have -ne $want) { Fail "download is damaged (checksum mismatch); try again" }

    $new = Join-Path $tmp "app"
    Expand-Archive -LiteralPath $archive -DestinationPath $new -Force
    if (-not (Test-Path (Join-Path $new "scripts\bootstrap.py"))) { Fail "the bundle is incomplete" }
    New-Item -ItemType Directory -Path $dest -Force | Out-Null
    try {
        # Copied over the old install so the launchers' Python environment survives.
        Copy-Item -Path (Join-Path $new "*") -Destination $dest -Recurse -Force
    } catch {
        Fail "could not replace files in $dest. Close Herdr (herdr server stop) and any agent windows, then run this again. $($_.Exception.Message)"
    }
} finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

$exe = $python[0]
$rest = @($python | Select-Object -Skip 1)
& $exe @rest (Join-Path $dest "scripts\bootstrap.py") @args
if ($LASTEXITCODE -ne 0) { Fail "setup did not finish (exit $LASTEXITCODE)" }
Write-Host ""
Write-Host "Done. Open a new terminal, then run: agent"
