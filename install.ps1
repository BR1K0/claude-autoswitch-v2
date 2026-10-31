# claude-autoswitch installer for Windows. Safe to run again. Everything lives in %USERPROFILE%\.autoswitch;
# uninstall.ps1 reverts it. No administrator rights are needed.
#
#   powershell -ExecutionPolicy Bypass -File install.ps1
#
# Environment (all optional):
#   AUTOSWITCH_DIR=...        install folder (default %USERPROFILE%\.autoswitch)
#   AUTOSWITCH_PORT=8765      port for the local scorer
#   AUTOSWITCH_NO_SERVICE=1   skip the scheduled task (the scorer is then not started)
#   AUTOSWITCH_TASK=...       scheduled task name (default AutoswitchScorer)
$ErrorActionPreference = 'Continue'

$Src = Split-Path -Parent $MyInvocation.MyCommand.Path
$Home_ = $env:USERPROFILE
$Base = if ($env:AUTOSWITCH_DIR) { $env:AUTOSWITCH_DIR } else { Join-Path $Home_ '.autoswitch' }
$App = Join-Path $Base 'app'
$Task = if ($env:AUTOSWITCH_TASK) { $env:AUTOSWITCH_TASK } else { 'AutoswitchScorer' }
$Version = (Get-Content (Join-Path $Src 'VERSION') -Raw).Trim()

function Say($m)  { Write-Host "==> $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "warning: $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "error: $m" -ForegroundColor Red; exit 1 }  # inside the main try: the finally below rolls back
function Write-Text($path, $text) { [IO.File]::WriteAllText($path, $text, (New-Object Text.UTF8Encoding($false))) }

# --- Python: a version scikit-learn 1.5.2 (requirements.txt) ships wheels for, 3.9 to 3.13. Without a wheel pip tries to
# compile scikit-learn from source, which fails on a normal PC. Preference: py -3.12, -3.11, -3.10, -3.13, -3.9, then
# python / python3 if they are in range; last resort py -3 / python / python3 of any 3.9+ version, with a warning.
# (The Microsoft Store stub fails the version check and is skipped.)
$PyMin = '(3, 9)'; $PyMaxExcl = '(3, 14)'
function Test-PyCandidate($c, $check) {
    if (-not (Get-Command $c[0] -ErrorAction SilentlyContinue)) { return $null }
    $rest = @(); if ($c.Count -gt 1) { $rest = $c[1..($c.Count - 1)] }
    $out = & $c[0] @rest -c "import sys; print(sys.executable); print('%d.%d' % sys.version_info[:2]); sys.exit(0 if $check else 3)" 2>$null
    if ($LASTEXITCODE -eq 0 -and $out -and @($out).Count -ge 2) {
        return @{ Exe = ([string]@($out)[0]).Trim(); Version = ([string]@($out)[1]).Trim(); Launcher = ($c -join ' ') }
    }
    return $null
}
function Find-Python {
    $supported = "$PyMin <= sys.version_info[:2] < $PyMaxExcl"
    foreach ($c in @(@('py', '-3.12'), @('py', '-3.11'), @('py', '-3.10'), @('py', '-3.13'), @('py', '-3.9'), @('python'), @('python3'))) {
        $r = Test-PyCandidate $c $supported; if ($r) { return $r }
    }
    foreach ($c in @(@('py', '-3'), @('python'), @('python3'))) {
        $r = Test-PyCandidate $c "sys.version_info[:2] >= $PyMin"
        if ($r) {
            Warn "only Python $($r.Version) found ($($r.Launcher)); scikit-learn 1.5.2 has no prebuilt wheels for it, so the scorer install will probably fail. Install Python 3.12 from https://www.python.org/downloads/ and run this again."
            return $r
        }
    }
    return $null
}

function Stop-Scorer {
    try { Stop-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue } catch {}
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -and $_.CommandLine -like "*$App*scorer.py*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Test-Health($port) {
    try { return ((Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 "http://127.0.0.1:$port/health").Content -eq 'ok') } catch { return $false }
}

$Py = Find-Python
if ($Py -and $Py.Version -and ([version]$Py.Version -ge [version]'3.14')) {
    Die "Python $($Py.Version) ($($Py.Launcher)) has no scikit-learn 1.5.2 wheels. Install Python 3.12 from https://www.python.org/downloads/ (it can sit next to $($Py.Version)) and run this again."
}
if (-not $Py) { Die 'Python 3.9 or newer not found. Install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH" and keep the py launcher), then run this again.' }
& $Py.Exe -c 'import venv, ensurepip' 2>$null
if ($LASTEXITCODE -ne 0) { Die 'This Python has no venv module. Reinstall Python from python.org.' }

# 1. copy the app (state files in $Base are kept)
Say "Installing to $Base"
Stop-Scorer
try {  # stop here if a copy fails (e.g. a file locked by antivirus) instead of carrying on with a half-copied app
    foreach ($d in @($Base, (Join-Path $Base 'bin'), (Join-Path $Base 'logs'))) { New-Item -ItemType Directory -Force $d -ErrorAction Stop | Out-Null }
    if (Test-Path $App) { Remove-Item -Recurse -Force $App -ErrorAction Stop }
    New-Item -ItemType Directory -Force $App -ErrorAction Stop | Out-Null
    foreach ($d in 'hook', 'scorer', 'train', 'data', 'bin') { Copy-Item -Recurse -Force (Join-Path $Src $d) (Join-Path $App $d) -ErrorAction Stop }
    foreach ($f in 'WATCHER.md', 'VERSION', 'requirements.txt') { Copy-Item -Force (Join-Path $Src $f) $App -ErrorAction Stop }
    Get-ChildItem $App -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
} catch { Die "could not copy the app to $App ($($_.Exception.Message))" }

# 1b. the hook, added to Claude Code's settings (backed up first; the old router hint is replaced, not stacked).
# Done right after the copy so an unreadable settings.json stops the install before the task and scorer are set up.
# Everything from here runs inside try/catch/finally. If any later step fails (a Die, a terminating error, or Ctrl-C,
# best effort), the finally puts settings.json, displaced.json and python-cmd back exactly as they were before this run:
# a fresh install leaves settings.json untouched, and a failed re-install or upgrade keeps the earlier working hook.
Say "Adding the hook to $Home_\.claude\settings.json"
$Cli = Join-Path $App 'bin\autoswitch'
& $Py.Exe $Cli _settings-snapshot
if ($LASTEXITCODE -ne 0) { Die 'could not save the current settings state; nothing was changed in settings.json' }
$Completed = $false
try {
    & $Py.Exe $Cli _settings-install ($Py.Exe) ($Py.Launcher)
    if ($LASTEXITCODE -ne 0) { Die 'could not update settings.json (see above)' }

    # 2. private virtualenv (only the scorer needs it; the hook itself uses just the standard library)
    $VenvPy = Join-Path $Base 'venv\Scripts\python.exe'
    $VenvPyW = Join-Path $Base 'venv\Scripts\pythonw.exe'
    $ReqStamp = Join-Path $Base 'venv\autoswitch-requirements.txt'
    $need = -not (Test-Path $VenvPy)
    if (-not $need) { & $VenvPy -c 'import sklearn' 2>$null; $need = ($LASTEXITCODE -ne 0) }
    if (-not $need) { $need = (-not (Test-Path $ReqStamp)) -or ((Get-Content (Join-Path $App 'requirements.txt') -Raw) -ne (Get-Content $ReqStamp -Raw)) }
    if ($need) {
        Say 'Creating virtualenv and installing scikit-learn (a few minutes the first time)'
        & $Py.Exe -m venv (Join-Path $Base 'venv')
        if ($LASTEXITCODE -ne 0) { Die 'could not create the virtualenv' }
        & $VenvPy -m pip install -q --disable-pip-version-check -r (Join-Path $App 'requirements.txt')
        if ($LASTEXITCODE -ne 0) { Die 'pip install failed (are you online?)' }
        Copy-Item -Force (Join-Path $App 'requirements.txt') $ReqStamp
        Remove-Item -Force (Join-Path $Base 'models\.version') -ErrorAction SilentlyContinue  # retrain against the pinned versions
    }

    # 3. train the two small models from the bundled public data
    $Models = Join-Path $Base 'models'
    $VerFile = Join-Path $Models '.version'
    $trained = if (Test-Path $VerFile) { (Get-Content $VerFile -Raw).Trim() } else { '' }
    if (-not (Test-Path (Join-Path $Models 'synthetic.pkl')) -or $trained -ne $Version) {
        Say 'Training the scorer on the bundled data (a few seconds)'
        & $VenvPy (Join-Path $App 'train\train.py') --out $Models | Out-Null
        if ($LASTEXITCODE -ne 0) { Die 'training failed' }
        Write-Text $VerFile "$Version`n"
    }

    # 4. screenshots: detected through the Windows clipboard (never blocked); the text inside them is not read on Windows yet
    Say 'Screenshots: detected and never blocked; their text is not read on Windows yet'

    # 5. port, mode and speed. An existing mode is kept; a fresh install starts in shadow (log only, change nothing).
    $PortFile = Join-Path $Base 'port'
    $rawPort = if ($env:AUTOSWITCH_PORT) { $env:AUTOSWITCH_PORT } elseif (Test-Path $PortFile) { Get-Content $PortFile -Raw } else { '' }
    $Port = 0
    if (-not [int]::TryParse(([string]$rawPort).Trim(), [ref]$Port) -or $Port -lt 1 -or $Port -gt 65535) {
        if ("$rawPort".Trim()) { Warn "ignoring invalid port '$("$rawPort".Trim())', using 8765" }
        $Port = 8765
    }
    function Test-PortFree($port) {
        & $Py.Exe -c "import socket,sys; s=socket.socket(); s.settimeout(0.3); sys.exit(0 if s.connect_ex(('127.0.0.1',$port))==0 else 1)"
        return ($LASTEXITCODE -ne 0)
    }
    if (-not (Test-PortFree $Port) -and -not (Test-Health $Port)) {
        while (-not (Test-PortFree $Port)) { $Port++ }
        Warn "default port was busy, using $Port"
    }
    Write-Text $PortFile "$Port`n"
    $ModeFile = Join-Path $Base 'mode'
    # a fresh install (no mode file yet) also gets speed=instant; an existing speed file, or an upgrade, is never touched
    if (-not (Test-Path $ModeFile)) {
        $SpeedFile = Join-Path $Base 'speed'
        if (-not (Test-Path $SpeedFile)) { Write-Text $SpeedFile "instant`n" }
        Write-Text $ModeFile "shadow`n"
    }

    # 6. scheduled task: starts the scorer at login (current user, limited rights, no administrator needed)
    $Scorer = Join-Path $App 'scorer\scorer.py'
    $StartupVbs = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\autoswitch-scorer.vbs'
    if (-not $env:AUTOSWITCH_NO_SERVICE -and -not $env:AUTOSWITCH_NO_LAUNCHD) {
        Say "Starting the scorer at login (scheduled task $Task)"
        $user = "$env:USERDOMAIN\$env:USERNAME"
        $args_ = "`"$Scorer`" --port $Port --models `"$Models`""
        $registered = $false
        try {
            $action = New-ScheduledTaskAction -Execute $VenvPyW -Argument $args_
            $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
            $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
            $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
                -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
            Register-ScheduledTask -TaskName $Task -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force -ErrorAction Stop | Out-Null
            $registered = $true
            Remove-Item -Force $StartupVbs -ErrorAction SilentlyContinue
            Start-ScheduledTask -TaskName $Task
        } catch {
            Warn "Task Scheduler refused ($($_.Exception.Message)); using a launcher in your Startup folder instead"
        }
        if (-not $registered) {
            $cmdline = ('"{0}" {1}' -f $VenvPyW, $args_).Replace('"', '""')
            Write-Text $StartupVbs ("CreateObject(`"WScript.Shell`").Run `"$cmdline`", 0, False`r`n")
            Start-Process -WindowStyle Hidden -FilePath $VenvPyW -ArgumentList $args_
        }
        for ($i = 0; $i -lt 40; $i++) { if (Test-Health $Port) { break }; Start-Sleep -Milliseconds 500 }
    }

    # 8. the autoswitch command: autoswitch.cmd in $Base\bin, which goes on your user PATH
    $Shim = Join-Path $Base 'bin\autoswitch.cmd'
    Write-Text $Shim ("@echo off`r`n`"{0}`" `"{1}`" %*`r`n" -f $Py.Exe, $Cli)
    $BinDir = (Join-Path $Base 'bin').TrimEnd('\')
    # Read the raw (unexpanded) value and write it back as REG_EXPAND_SZ, so entries like %USERPROFILE%\bin stay as they are.
    $envKey = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
    $userPath = [string]$envKey.GetValue('Path', '', 'DoNotExpandEnvironmentNames')
    $parts = @(); if ($userPath) { $parts = @($userPath -split ';' | Where-Object { $_ }) }
    if (-not ($parts | Where-Object { [Environment]::ExpandEnvironmentVariables($_).TrimEnd('\') -ieq $BinDir })) {
        $envKey.SetValue('Path', (($parts + $BinDir) -join ';'), [Microsoft.Win32.RegistryValueKind]::ExpandString)
        Say "Added $BinDir to your user PATH (open a new terminal to use the autoswitch command)"
    }
    $envKey.Close()
    if (-not (($env:Path -split ';') | Where-Object { $_.TrimEnd('\') -ieq $BinDir })) { $env:Path = "$env:Path;$BinDir" }

    # 9. smoke test: the hook must answer through the scorer
    if (-not $env:AUTOSWITCH_NO_SERVICE -and -not $env:AUTOSWITCH_NO_LAUNCHD) {
        try {
            $r = Invoke-RestMethod -UseBasicParsing -Method Post -TimeoutSec 5 -Uri "http://127.0.0.1:$Port/" -ContentType 'application/json' -Body '{"text":"what does git rebase do"}'
            Say "Scorer answered: $($r.label)"
        } catch { Warn "scorer POST failed: $($_.Exception.Message)" }
        $old = $env:AUTOSWITCH_MODE; $oldDbg = $env:AUTOSWITCH_DEBUG
        $env:AUTOSWITCH_MODE = 'hint'; $env:AUTOSWITCH_DEBUG = '1'
        $out = '{"prompt":"what does git rebase do","session_id":"install-check"}' | & $Py.Exe (Join-Path $App 'hook\hint.py') 2>&1
        $env:AUTOSWITCH_MODE = $old; $env:AUTOSWITCH_DEBUG = $oldDbg
        if ("$out" -match 'Router suggests') { Say "Self-test passed: $out" } else { Warn "self-test got no answer from the scorer: $($out -join "`n"); see $Base\logs\scorer.log" }
    }

    $Completed = $true
} catch {
    Write-Host "error: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
} finally {
    if (-not $Completed) {
        & $Py.Exe $Cli _settings-rollback
        if ($LASTEXITCODE -ne 0) { Write-Host 'Could not put settings.json back; run uninstall.ps1.' -ForegroundColor Yellow }
        Write-Host 'Install did not finish. Fix the problem above and run install.ps1 again, or run uninstall.ps1 to remove the rest.' -ForegroundColor Yellow
    } else {
        # success: mark the snapshot committed (renamed to install-snapshot.done), so the next install/uninstall
        # never undoes this good install. Only a snapshot left by a hard kill is still undone by them.
        & $Py.Exe $Cli _settings-commit
        if ($LASTEXITCODE -ne 0) { Write-Host "Could not mark the install as finished; delete $Base\install-snapshot.json before the next install." -ForegroundColor Yellow }
    }
}

Write-Host ''
Write-Host "Installed claude-autoswitch $Version (Windows) in shadow mode: it logs what it would switch and changes nothing." -ForegroundColor Green
Write-Host @'

  autoswitch report     what it would have done on your real prompts (after some use)
  autoswitch on         switch for real   (also: off | shadow | hint | status)
  autoswitch speed      instant (default on fresh installs) or seamless, see the README

To switch on, you need one Watcher chat (details below).

'@
& $Py.Exe $Cli watcher
Write-Host ''
Write-Host 'Restart Claude Code (or open a new chat) so it picks up the hook. Remove everything with uninstall.ps1'
