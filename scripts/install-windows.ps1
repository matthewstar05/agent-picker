param([string]$Python)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
if (-not $Python) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $Python = (& py -3 -c 'import sys; print(sys.executable)')
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        $Python = (& python -c 'import sys; print(sys.executable)')
    } else { throw 'Install Python 3.9+ first, then rerun this script.' }
}
if (-not (Test-Path -LiteralPath $Python)) { throw 'Python executable was not found.' }
& $Python -c 'import sys; assert sys.version_info >= (3, 9), "Python 3.9+ required"'
if ($LASTEXITCODE -ne 0) { throw 'Python validation failed.' }
$entry = Join-Path $repo 'src\agent_picker.py'
& $Python $entry --picker-doctor
if ($LASTEXITCODE -ne 0) { throw 'Picker configuration validation failed.' }
$configDir = Join-Path $env:APPDATA 'agent-picker'
$stateDir = Join-Path $env:LOCALAPPDATA 'agent-picker'
if ($env:AGENT_PICKER_STATE_HOME) { $stateDir = $env:AGENT_PICKER_STATE_HOME }
$configPath = Join-Path $configDir 'config.json'
if ($env:AGENT_PICKER_CONFIG) { $configPath = $env:AGENT_PICKER_CONFIG }
$utf8 = New-Object System.Text.UTF8Encoding($false)
function Save-Backup([string]$Path) {
    if (Test-Path -LiteralPath $Path) {
        $dir = Join-Path $stateDir ('backups\' + (Get-Date -Format 'yyyyMMdd-HHmmss-fffffff'))
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        Copy-Item -LiteralPath $Path -Destination $dir
        Write-Host "Backup: $dir"
    }
}
# Capture Cursor before our PowerShell function takes the generic command name.
# Do not move vendor executables: their updater still owns those files.
$vendor = Get-Command cursor-agent,cursor-cli -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $vendor) { $vendor = Get-Command agent -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1 }
if ($vendor -and $vendor.Source -notlike '*\agent-picker\bin\*') {
    $config = @{ agents = @{} }
    if (Test-Path -LiteralPath $configPath) {
        $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    }
    if ($config -is [hashtable]) {
        $config.agents.Cursor = @{ command = $vendor.Source }
    } elseif (-not $config.agents) {
        $config | Add-Member -NotePropertyName agents -NotePropertyValue ([pscustomobject]@{})
        $config.agents | Add-Member -NotePropertyName Cursor -NotePropertyValue @{ command = $vendor.Source }
    } elseif (-not $config.agents.Cursor) {
        $config.agents | Add-Member -NotePropertyName Cursor -NotePropertyValue @{ command = $vendor.Source }
    }
    Save-Backup $configPath
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $configPath) | Out-Null
    [IO.File]::WriteAllText($configPath, ($config | ConvertTo-Json -Depth 20) + "`n", $utf8)
}
$binDir = Join-Path $env:LOCALAPPDATA 'agent-picker\bin'
New-Item -ItemType Directory -Force -Path $binDir | Out-Null
# The PowerShell function is the primary interface and forwards args as an array.
# A separate .cmd launcher also supports command prompt with normal cmd quoting.
$cmdPath = Join-Path $binDir 'agent-picker.cmd'
$cmd = "@echo off`r`n`"$Python`" `"$entry`" %*`r`nexit /b %errorlevel%`r`n"
if (-not (Test-Path -LiteralPath $cmdPath) -or [IO.File]::ReadAllText($cmdPath) -ne $cmd) {
    Save-Backup $cmdPath
    [IO.File]::WriteAllText($cmdPath, $cmd, $utf8)
}
# herdr-ai opens a herdr tab with every agent, the selector, and the usage panel.
$herdrAiPath = Join-Path $binDir 'herdr-ai.cmd'
$herdrAi = "@echo off`r`n`"$Python`" `"$(Join-Path $repo 'src\herdr_ai.py')`" %*`r`nexit /b %errorlevel%`r`n"
if (-not (Test-Path -LiteralPath $herdrAiPath) -or [IO.File]::ReadAllText($herdrAiPath) -ne $herdrAi) {
    Save-Backup $herdrAiPath
    [IO.File]::WriteAllText($herdrAiPath, $herdrAi, $utf8)
}
$escapedPython = $Python.Replace("'", "''")
$escapedEntry = $entry.Replace("'", "''")
$escapedHerdrAi = (Join-Path $repo 'src\herdr_ai.py').Replace("'", "''")
$start = '# >>> agent-picker managed >>>'
$end = '# <<< agent-picker managed <<<'
$block = @"
$start
Remove-Item Alias:agent -ErrorAction SilentlyContinue
Remove-Item Alias:agent-picker -ErrorAction SilentlyContinue
function global:agent { & '$escapedPython' '$escapedEntry' @args }
function global:agent-picker { & '$escapedPython' '$escapedEntry' @args }
function global:usage { & '$escapedPython' '$escapedEntry' @args }
function global:herdr-ai { & '$escapedPython' '$escapedHerdrAi' @args }
$end
"@
$profiles = @($PROFILE.CurrentUserAllHosts)
foreach ($folder in @('PowerShell', 'WindowsPowerShell')) {
    $profiles += Join-Path ([Environment]::GetFolderPath('MyDocuments')) "$folder\profile.ps1"
}
foreach ($path in ($profiles | Select-Object -Unique)) {
    $old = if (Test-Path -LiteralPath $path) { [IO.File]::ReadAllText($path) } else { '' }
    $new = [regex]::Replace($old, [regex]::Escape($start) + '[\s\S]*?' + [regex]::Escape($end) + '\r?\n?', '').TrimEnd() + "`r`n`r`n" + $block + "`r`n"
    if ($new -ne $old) {
        Save-Backup $path
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $path) | Out-Null
        [IO.File]::WriteAllText($path, $new, $utf8)
    }
}
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (($userPath -split ';') -notcontains $binDir) {
    [Environment]::SetEnvironmentVariable('Path', "$binDir;$userPath", 'User')
}
$env:PATH = "$binDir;$env:PATH"
Invoke-Expression $block
& $Python $entry --picker-doctor
if ($LASTEXITCODE -ne 0) { throw 'Installed picker validation failed.' }
if (-not (Get-Command herdr -ErrorAction SilentlyContinue) -and -not (Test-Path (Join-Path $env:LOCALAPPDATA 'Programs\Herdr\bin\herdr.exe'))) {
    Write-Host 'herdr is not installed; herdr-ai needs it. scripts\setup-windows.ps1 installs it.'
}
Write-Host 'Installed. Run agent --picker-doctor, then agent in Windows Terminal.'
Write-Host "Updates are read directly from: $repo"
