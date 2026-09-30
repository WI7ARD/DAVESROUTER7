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

    With -SuiteDir (boards from tests/fixtures/benchmark_suite.py write_suite), the
    installed pcbrouter.exe also routes real boards end to end: 2-layer single worker,
    2- and 4-layer with parallel helpers, an impossible board (must fail clearly, not
    hang), a malformed file (message, no traceback). Each output must reopen and pass
    the internal check, and every source board must be byte-for-byte unchanged. The
    GUI is started once with a corrupt settings file (must recover, keep a backup).
#>
param(
    [string]$Setup = (Get-ChildItem "$PSScriptRoot\..\..\dist\installer\*-Setup-x64.exe" |
        Select-Object -First 1).FullName,
    [string]$Board = (Resolve-Path "$PSScriptRoot\..\..\tests\fixtures\boards\can_node.kicad_pcb").Path,
    [string]$SuiteDir = ''
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
foreach ($doc in 'QUICK_START.md', 'USER_GUIDE.md', 'TROUBLESHOOTING.md', 'KNOWN_LIMITATIONS.md') {
    Check (@(Get-ChildItem $dir -Recurse -Filter $doc).Count -ge 1) "user documentation installed: $doc"
}

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
$gpuText = (& $cli --gpu-check) -join "`n"
Write-Host $gpuText
$gpu = $gpuText | ConvertFrom-Json
Check ($gpu.library -eq 'dpnp' -and $gpu.library_loads) "installed app loads bundled dpnp (see the report above)"
# Staged SYCL diagnostic in a crash-isolated child of the installed exe. The CI
# machine has no Intel GPU: the SYCL runtime must load and the verdict must say
# where it stopped (no crash, no hang, no false "GPU ready").
$stage = @{}
foreach ($s in $gpu.diagnostic.stages) { $stage[$s.name] = $s.ok }
Check ($stage['dpctl_import'] -and $stage['dpnp_import']) "SYCL runtime (dpctl + dpnp) loads in the installed app"
Check ($gpu.verdict -and $gpu.verdict -notmatch 'crashed|hung|could not start') "GPU diagnostic verdict: $($gpu.verdict)"
Check ($gpu.gpu_ready -eq [bool]$stage['fused_kernel']) "gpu_ready only when the routing kernel ran and was verified ($($gpu.gpu_ready))"


# ---------------------------------------------------------------- product routing
if ($SuiteDir) {
    $suite = (Resolve-Path $SuiteDir).Path
    $out = Join-Path $env:TEMP 'pcbrouter-smoke-routes'
    New-Item -ItemType Directory -Force -Path $out | Out-Null

    function Route-Board([string]$Name, [string]$Mode, [int]$Workers, [int]$Budget) {
        $src = Join-Path $suite "$Name.kicad_pcb"
        $before = (Get-FileHash $src -Algorithm SHA256).Hash
        $dst = Join-Path $out "$Name-$Mode-w$Workers.kicad_pcb"
        $rep = Join-Path $out "$Name-$Mode-w$Workers.json"
        $sw = [Diagnostics.Stopwatch]::StartNew()
        $job = Start-Process -FilePath $cli -PassThru -NoNewWindow `
            -RedirectStandardOutput "$rep.log" -RedirectStandardError "$rep.err" `
            -ArgumentList "`"$src`" --route --mode $Mode --workers $Workers --budget $Budget --output `"$dst`" --report `"$rep`""
        if (-not $job.WaitForExit(($Budget + 240) * 1000)) {
            Stop-Process -Id $job.Id -Force
            Get-Content "$rep.log" -Tail 30 | Write-Host
            throw "FAILED: $Name ($Mode, workers $Workers) hung past its budget"
        }
        $sw.Stop()
        Get-Content "$rep.log" -Tail 8 | Write-Host
        Check ((Get-FileHash $src -Algorithm SHA256).Hash -eq $before) "$Name source board unchanged"
        $r = Get-Content $rep -Raw | ConvertFrom-Json
        $r | Add-Member -NotePropertyName wall_s -NotePropertyValue ([math]::Round($sw.Elapsed.TotalSeconds, 1))
        $r | Add-Member -NotePropertyName exit -NotePropertyValue $job.ExitCode
        $r | Add-Member -NotePropertyName out_path -NotePropertyValue $dst
        return $r
    }

    function Check-Output($r, [string]$What) {
        Check (Test-Path $r.out_path) "$What wrote a new board"
        $again = ((& $cli $r.out_path --inspect --no-log-file) -join "`n") | ConvertFrom-Json
        Check ($LASTEXITCODE -eq 0 -and $again.counts.tracks -gt 0) "$What output reopens ($($again.counts.tracks) tracks, $($again.counts.vias) vias)"
        Check ($r.verified -eq $true) "$What output passes the internal geometry check"
    }

    # Minimum completion (nets) per board: the product must route these fully or
    # nearly so. Numbers come from local runs of tools/run_benchmarks.py.
    $r = Route-Board 'tiny' 'accuracy' 0 120
    Check ($r.exit -eq 0 -and $r.metrics.nets_completed -eq $r.metrics.nets_attempted) "tiny 2-layer, single worker: $($r.summary) ($($r.wall_s) s)"
    Check-Output $r 'tiny'

    $r = Route-Board 'medium_2layer' 'speed' 0 300
    Check ($r.exit -eq 0 -and $r.metrics.nets_completed -eq $r.metrics.nets_attempted) "medium 2-layer, single worker: $($r.summary) ($($r.wall_s) s)"
    Check-Output $r 'medium 2-layer'

    # Parallel routing must really run (helpers started, results committed through
    # the validator) on a board whose nets are spread out: four circuit blocks.
    $r = Route-Board 'modular_4layer' 'accuracy' -1 300
    Check ($r.layers.Count -eq 4) '4-layer board read with 4 copper layers'
    Check ($r.metrics.parallel_batches -gt 0) "parallel helpers routed nets ($($r.metrics.parallel_batches) results, $($r.metrics.parallel_conflicts) conflicts; $($r.log -join '; '))"
    Check ($r.exit -eq 0 -and $r.metrics.nets_completed -eq $r.metrics.nets_attempted) "modular 4-layer, parallel: $($r.summary) ($($r.wall_s) s)"
    Check-Output $r 'modular 4-layer'

    $r = Route-Board 'medium_4layer' 'speed' 0 300
    Check ($r.layers.Count -eq 4) '4-layer board read with 4 copper layers'
    Check ($r.exit -in 0, 3 -and $r.metrics.nets_completed -ge [math]::Floor(0.85 * $r.metrics.nets_attempted)) "medium 4-layer (0.5 mm QFP), Speed: $($r.summary) ($($r.wall_s) s)"
    Check-Output $r 'medium 4-layer'

    $r = Route-Board 'impossible' 'accuracy' 0 60
    Check ($r.exit -ne 0 -and $r.metrics.nets_completed -eq 0) "impossible board fails clearly (exit $($r.exit)): $($r.message)"
    Check (($r.failed_nets.PSObject.Properties | Measure-Object).Count -eq 1) "impossible board names the failed net: $(($r.failed_nets.PSObject.Properties | Select-Object -First 1).Value)"

    $bad = Join-Path $out 'malformed.kicad_pcb'
    Set-Content -Path $bad -Value '(kicad_pcb (version 20240108) (net 0 "") (footprint' -Encoding ascii
    $msg = (& $cli $bad --route --output (Join-Path $out 'malformed_out.kicad_pcb') 2>&1) -join "`n"
    Check ($LASTEXITCODE -eq 1 -and $msg -match 'Could not read' -and $msg -notmatch 'Traceback') "malformed board gives a message: $msg"
    Check (-not (Test-Path (Join-Path $out 'malformed_out.kicad_pcb'))) 'malformed board writes no output'
}

# ---------------------------------------------------------------- GUI
# Start with a corrupt settings file: the app must recover (defaults, backup kept).
$cfgDir = Join-Path $env:APPDATA 'AI PCB Router'
New-Item -ItemType Directory -Force -Path $cfgDir | Out-Null
Set-Content -Path (Join-Path $cfgDir 'settings.json') -Value '{"routing": {"parallel_workers": "lots"' -Encoding ascii
$gui = Start-Process -FilePath (Join-Path $dir 'AI PCB Router.exe') -ArgumentList "`"$Board`"" -PassThru
# Cold machines page in a ~360 MB bundle under antivirus watch: poll for the
# board load instead of a fixed sleep (fast when warm, patient when cold).
$guiLog = Join-Path $dataDir 'logs\pcbrouter.log'
$deadline = (Get-Date).AddSeconds(180)
while (-not $gui.HasExited) {
    if ((Test-Path $guiLog) -and (Select-String -Path $guiLog -Pattern 'board\.load\.done' -Quiet)) {
        break
    }
    if ((Get-Date) -gt $deadline) {
        break
    }
    Start-Sleep -Seconds 2
}
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
Check (@(Get-ChildItem $cfgDir -Filter 'settings.json.corrupt-*').Count -ge 1) 'corrupt settings file set aside, app started with defaults'

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
# Checks above failed by throwing; a native command's exit code (e.g. the malformed
# board correctly exiting 1) must not become this script's result.
exit 0
