# Fully reverts install.ps1: stops the scorer, removes the scheduled task and the hook (restoring any hook it replaced),
# the autoswitch command and %USERPROFILE%\.autoswitch. Use -KeepData to keep your shadow log and settings there.
#
#   powershell -ExecutionPolicy Bypass -File uninstall.ps1 [-KeepData]
param([switch]$KeepData)
$ErrorActionPreference = 'Continue'

$Src = Split-Path -Parent $MyInvocation.MyCommand.Path
$Base = if ($env:AUTOSWITCH_DIR) { $env:AUTOSWITCH_DIR } else { Join-Path $env:USERPROFILE '.autoswitch' }
$App = Join-Path $Base 'app'
$Task = if ($env:AUTOSWITCH_TASK) { $env:AUTOSWITCH_TASK } else { 'AutoswitchScorer' }
$Cli = Join-Path $App 'bin\autoswitch'
if (-not (Test-Path $Cli)) { $Cli = Join-Path $Src 'bin\autoswitch' }

function Find-Python {
    # same preference as install.ps1 (uninstall only needs the standard library, so any 3.9+ will do)
    foreach ($c in @(@('py', '-3.12'), @('py', '-3.11'), @('py', '-3.10'), @('py', '-3.13'), @('py', '-3.9'), @('py', '-3'), @('python'), @('python3'))) {
        if (-not (Get-Command $c[0] -ErrorAction SilentlyContinue)) { continue }
        $rest = @(); if ($c.Count -gt 1) { $rest = $c[1..($c.Count - 1)] }
        $out = & $c[0] @rest -c "import sys; print(sys.executable); sys.exit(0 if sys.version_info >= (3, 9) else 3)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) { return ([string]($out | Select-Object -First 1)).Trim() }
    }
    return $null
}

$Py = Find-Python
if (-not $Py) { Write-Host 'error: Python 3.9+ not found; it is needed to restore settings.json. Nothing was removed.' -ForegroundColor Red; exit 1 }

Write-Host "==> Removing the hook from $env:USERPROFILE\.claude\settings.json"
& $Py $Cli _settings-uninstall
if ($LASTEXITCODE -ne 0) {
    Write-Host 'error: could not restore settings.json; nothing was removed. Fix the problem above and run uninstall.ps1 again.' -ForegroundColor Red
    exit 1
}

# the autoswitch-inline plugin and its marketplace, if `autoswitch speed inline` installed them (as uninstall.sh does)
& $Py $Cli _plugin-uninstall

Write-Host '==> Stopping the scorer'
try { Stop-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue } catch {}
try { Unregister-ScheduledTask -TaskName $Task -Confirm:$false -ErrorAction SilentlyContinue } catch {}
Remove-Item -Force (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\autoswitch-scorer.vbs') -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -and $_.CommandLine -like "*$App*scorer.py*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

$BinDir = (Join-Path $Base 'bin').TrimEnd('\')
# Raw (unexpanded) value in, REG_EXPAND_SZ out, so %VAR% entries in the user PATH are kept as they are.
$envKey = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
if ($envKey) {
    $userPath = [string]$envKey.GetValue('Path', '', 'DoNotExpandEnvironmentNames')
    if ($userPath) {
        $kept = @($userPath -split ';' | Where-Object { $_ -and ([Environment]::ExpandEnvironmentVariables($_).TrimEnd('\') -ine $BinDir) })
        if ($kept.Count -ne @($userPath -split ';' | Where-Object { $_ }).Count) {
            $envKey.SetValue('Path', ($kept -join ';'), [Microsoft.Win32.RegistryValueKind]::ExpandString)
        }
    }
    $envKey.Close()
}

if ($KeepData) {
    foreach ($d in 'app', 'venv', 'models', 'bin') { Remove-Item -Recurse -Force (Join-Path $Base $d) -ErrorAction SilentlyContinue }
    Write-Host "==> Kept your data in $Base"
} else {
    Remove-Item -Recurse -Force $Base -ErrorAction SilentlyContinue
}
Write-Host 'Done. Close the Watcher chat if it is still open. Backups of settings.json (settings.json.autoswitch-backup-*) were left in ~\.claude; delete them if you like.'
