# install_autostart.ps1 — register the dashboards + Stage-0 collector to start at logon
# on THIS Windows computer. Self-locating; idempotent (re-run any time to refresh paths).
#
#   powershell -ExecutionPolicy Bypass -File scripts\windows\install_autostart.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\windows\install_autostart.ps1 -Uninstall
#
# Registers two Scheduled Tasks (current user, at logon, hidden window):
#   PolybotDashboards -> py scripts\run_dashboards.py     (crypto :5053, weather :5054)
#   PolybotStage0     -> py scripts\stage0_collector.py collect
# Logs land in the repo's logs\ folder (each child writes its own log).
param([switch]$Uninstall)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

$tasks = @(
    @{ Name = "PolybotDashboards"; Args = "scripts\run_dashboards.py" },
    @{ Name = "PolybotStage0";     Args = "scripts\stage0_collector.py collect" }
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
    $action  = New-ScheduledTaskAction -Execute $py -Argument $t.Args -WorkingDirectory $RepoRoot
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:UserName
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 3650)
    Register-ScheduledTask -TaskName $t.Name -Action $action -Trigger $trigger `
        -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName $t.Name
    Write-Host "installed + started $($t.Name)"
}

Write-Host ""
Write-Host "Done. Both run now and at every logon (background, no window)."
Write-Host "  dashboards : http://127.0.0.1:5053  and  http://127.0.0.1:5054"
Write-Host "  collector  : py scripts\stage0_collector.py report   (check after a few hours)"
Write-Host "  status     : Get-ScheduledTask PolybotDashboards,PolybotStage0"
Write-Host "  remove     : rerun this script with -Uninstall"
