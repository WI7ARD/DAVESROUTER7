<#
.SYNOPSIS
    Installs the built setup silently, exercises the installed app, then uninstalls it.

.DESCRIPTION
    Used by .github/workflows/windows-installer.yml on a real Windows machine. Safe to
    run locally: the install is per-user, and the script removes it again (including the
    app's settings and logs, so do not run it on a machine where you use the app).

    Checks: silent install, Installed-apps entry, shortcuts, the frozen bundle
    (--diagnostics: Qt, both AI SDKs, keyring, Windows Credential Manager), board
    inspection through the CLI, the GUI starting and opening a board, the uninstaller
    refusing to run while the app is open, and a clean uninstall.
#>
param(
    [string]$Setup = (Get-ChildItem "$PSScriptRoot\..\..\dist\installer\*-Setup-x64.exe" |
        Select-Object -First 1).FullName,
    [string]$Board = (Resolve-Path "$PSScriptRoot\..\..\tests\fixtures\boards\can_node.kicad_pcb").Path
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Check([bool]$Condition, [string]$What) {
    if (-not $Condition) { throw "FAILED: $What" }
    Write-Host "ok   $What"
}

$dir = Join-Path $env:LOCALAPPDATA 'Programs\AI PCB Router'
$dataDir = Join-Path $env:LOCALAPPDATA 'AI PCB Router'
$uninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\AI PCB Router'
$startMenuLink = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\AI PCB Router.lnk'
$cli = Join-Path $dir 'pcbrouter.exe'

# ---------------------------------------------------------------- install
Check (Test-Path $Setup) "installer found: $Setup"
$p = Start-Process -FilePath $Setup -ArgumentList '/S' -Wait -PassThru
Check ($p.ExitCode -eq 0) "silent install exit code 0 (got $($p.ExitCode))"
Check (Test-Path (Join-Path $dir 'AI PCB Router.exe')) 'GUI executable installed'
Check (Test-Path $cli) 'CLI executable installed'
Check (Test-Path (Join-Path $dir 'Uninstall.exe')) 'uninstaller written'
$reg = Get-ItemProperty $uninstallKey
Check ($reg.DisplayName -eq 'AI PCB Router') 'Installed apps entry'
Check (Test-Path $startMenuLink) 'Start menu shortcut'

# ---------------------------------------------------------------- CLI / bundle
$version = (& $cli --version) -join ''
Check ($LASTEXITCODE -eq 0 -and $version -match '^AI PCB Router ') "--version: $version"
Check ($reg.DisplayVersion -eq ($version -replace '^AI PCB Router ', '')) 'registry version matches the app'

$diagText = (& $cli --diagnostics) -join "`n"
Check ($LASTEXITCODE -eq 0) '--diagnostics runs'
Write-Host $diagText
$diag = $diagText | ConvertFrom-Json
Check ($diag.frozen) 'running from the frozen bundle'
Check ($diag.qt.available) "Qt loads (Qt $($diag.qt.qt))"
foreach ($sdk in 'openai', 'anthropic') {
    $s = $diag.packages.$sdk
    Check ($s.available -and $s.client -eq 'ok') "$sdk SDK bundled and its client builds (v$($s.version))"
}
Check ($diag.packages.keyring.available) 'keyring bundled'
Check ($diag.credential_store.secure_available) "secure key storage: $($diag.credential_store.description)"

$inspect = ((& $cli $Board --inspect --no-log-file) -join "`n") | ConvertFrom-Json
Check ($LASTEXITCODE -eq 0 -and $inspect.counts.nets -eq 7) "CLI inspects a board ($($inspect.counts.nets) nets)"

# Routing worker process in the frozen build: spawn + freeze_support must turn the
# child invocation into the worker (not a second app), and a real route must run.
$selftest = ((& $cli $Board --worker-selftest) -join "`n") | ConvertFrom-Json
Check ($LASTEXITCODE -eq 0 -and $selftest.ok) "routing worker process runs in the installed app (fake: $($selftest.fake_job.status), board: $($selftest.board_job.status))"

# Intel GPU support bundled into the installed app: dpnp + the SYCL runtime DLLs must
# load from the bundle (the CI machine has no Intel GPU, so no device is expected).
$gpu = ((& $cli --gpu-check) -join "`n") | ConvertFrom-Json
Check ($gpu.library -eq 'dpnp' -and $gpu.library_loads) "installed app loads bundled dpnp ($($gpu.library_version); $($gpu.problem))"

# ---------------------------------------------------------------- GUI
$gui = Start-Process -FilePath (Join-Path $dir 'AI PCB Router.exe') -ArgumentList "`"$Board`"" -PassThru
Start-Sleep -Seconds 12
Check (-not $gui.HasExited) 'GUI starts and keeps running'

# The uninstaller must refuse while the app runs. With _?= it runs in place and
# synchronously, so its exit code is observable (5 = app running).
$u = Start-Process -FilePath (Join-Path $dir 'Uninstall.exe') -ArgumentList "/S _?=$dir" -Wait -PassThru
Check ($u.ExitCode -eq 5) "uninstall refused while the app is running (exit $($u.ExitCode))"
Check (Test-Path (Join-Path $dir 'AI PCB Router.exe')) 'program files kept while running'

Stop-Process -Id $gui.Id -Force
$gui.WaitForExit()
$log = Get-Content (Join-Path $dataDir 'logs\pcbrouter.log') -Raw
Check ($log -match 'board\.load\.done') 'GUI opened the board (log)'
Check ($log -notmatch 'unhandled_exception') 'no unhandled exceptions in the log'

# ---------------------------------------------------------------- uninstall
# Without _?= the uninstaller copies itself to %TEMP% and returns at once: poll.
Start-Process -FilePath (Join-Path $dir 'Uninstall.exe') -ArgumentList '/S /REMOVEUSERDATA' -Wait | Out-Null
$deadline = (Get-Date).AddSeconds(120)
while ((Test-Path $dir) -and ((Get-Date) -lt $deadline)) { Start-Sleep -Seconds 1 }
Check (-not (Test-Path $dir)) 'install folder removed'
Check (-not (Test-Path $uninstallKey)) 'Installed apps entry removed'
Check (-not (Test-Path $startMenuLink)) 'Start menu shortcut removed'
Check (-not (Test-Path $dataDir)) 'logs and workspaces removed on request'

Write-Host 'Smoke test passed.'
