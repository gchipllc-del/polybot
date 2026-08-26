# install_autostart.ps1 — register the dashboards + Stage-0 collector to start at logon
# on THIS Windows computer. Self-locating; idempotent (re-run any time to refresh paths).
#
#   powershell -ExecutionPolicy Bypass -File scripts\windows\install_autostart.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\windows\install_autostart.ps1 -Uninstall
#
# Registers two Scheduled Tasks (current user, at logon, hidden window):
#   PolybotDashboards -> py scripts\run_dashboards.py     (crypto :5153, weather :5154)
#   PolybotStage0     -> py scripts\stage0_collector.py collect
# Logs land in the repo's logs\ folder (each child writes its own log).
param([switch]$Uninstall)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

$tasks = @(
    @{ Name = "PolybotDashboards"; Args = "scripts\run_dashboards.py" },
    @{ Name = "PolybotStage0";     Args = "scripts\stage0_collector.py collect" },
    @{ Name = "PolybotPaper";      Args = "scripts\paper_trader.py run" },
    # Watchdog: every 15 min, prove the system is actually WORKING (not merely alive)
    # and restart anything dead. This is the layer that makes breakage self-correcting
    # instead of something you discover days later.
    @{ Name = "PolybotHealth";     Args = "scripts\healthcheck.py --repair --quiet --log --fast";
       Every = 15 },
    # Hermes: daily bounded-optimizer review. READ-ONLY - it can only write proposals;
    # activating one always requires a human running `hermes.py apply <name>`.
    @{ Name = "PolybotHermes";     Args = "scripts\hermes.py review";
       Every = 1440 }
)

if ($Uninstall) {
    foreach ($t in $tasks) {
        Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "removed $($t.Name)"
    }
    exit 0
}

# find the python launcher (py.exe) or python.exe
$py = (Get-Command py -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $py) { Write-Error "python not found on PATH"; exit 1 }

Write-Host "repo   : $RepoRoot"
Write-Host "python : $py"

foreach ($t in $tasks) {
    $action = New-ScheduledTaskAction -Execute $py -Argument $t.Args -WorkingDirectory $RepoRoot

    # TWO triggers, because AtLogOn alone is not enough: the doctor caught every task
    # sitting in state "Ready" (i.e. dead) after a process exit, silently stopping data
    # collection. The second trigger re-fires every 10 minutes forever, and
    # -MultipleInstances IgnoreNew makes that a no-op while the task is alive. Net
    # effect: anything that dies is back within 10 minutes, no babysitting.
    $tLogon  = New-ScheduledTaskTrigger -AtLogOn -User $env:UserName
    $everyMin = if ($t.Every) { [int]$t.Every } else { 10 }
    $tRepeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes $everyMin)
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit (New-TimeSpan -Days 3650) -MultipleInstances IgnoreNew `
        -StartWhenAvailable
    Register-ScheduledTask -TaskName $t.Name -Action $action -Trigger $tLogon, $tRepeat `
        -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName $t.Name
    Write-Host "installed + started $($t.Name)"
}

Write-Host ""
Write-Host "Done. All three run now, at every logon, and self-heal every 10 min if they die."
Write-Host "  dashboards : http://127.0.0.1:5153  and  http://127.0.0.1:5154"
Write-Host "  collector  : py scripts\stage0_collector.py report"
Write-Host "  paper      : py scripts\paper_trader.py report    (forward-only, no real orders)"
Write-Host "  shadow     : py scripts\shadow_book.py report"
Write-Host "  status     : Get-ScheduledTask PolybotDashboards,PolybotStage0,PolybotPaper"
Write-Host "  health     : py scripts\healthcheck.py           (add --repair to fix)"
Write-Host "  diagnose   : py scripts\run_dashboards.py --doctor"
Write-Host "  remove     : rerun this script with -Uninstall"
