<#
Registers a daily Windows Scheduled Task that runs voucher_audit_skill\scripts\daily_run.py
at 12:00 PM, only when the PC is on and the user is logged in. Single-pass --
runs once per day and exits when all in-process claims have been audited.

Run this script ONCE from an elevated PowerShell:
    cd "D:\Company Projects\Voucher_Audit_Skill"
    powershell -ExecutionPolicy Bypass -File scripts\install_scheduled_task.ps1

It picks up the venv python.exe automatically if .venv\Scripts\python.exe exists,
otherwise falls back to the system python on PATH.

To uninstall later:
    Unregister-ScheduledTask -TaskName "VoucherAudit_DailyRun" -Confirm:$false
#>

$ErrorActionPreference = 'Stop'

$skillRoot = (Resolve-Path "$PSScriptRoot\..").Path
$dailyRun  = Join-Path $skillRoot 'scripts\daily_run.py'

# Pick the venv python first (matches the env where the user installed deps)
$venvPython = Join-Path $skillRoot '.venv\Scripts\python.exe'
$pyExe = if (Test-Path $venvPython) { $venvPython } else { 'python.exe' }

Write-Host "Skill root : $skillRoot"
Write-Host "Python     : $pyExe"
Write-Host "Daily run  : $dailyRun"

$action = New-ScheduledTaskAction `
    -Execute $pyExe `
    -Argument "`"$dailyRun`"" `
    -WorkingDirectory $skillRoot

# Daily at 12:00 PM. -At creates a noon trigger.
$trigger = New-ScheduledTaskTrigger -Daily -At 12:00pm

# Settings: only when AC powered AND user logged on, do not start if missed,
# stop after 4 hours if it gets stuck, allow demand start.
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4) `
    -MultipleInstances IgnoreNew

# Run as the currently logged-in user, only when logged in (skill needs Chrome
# session for SpineHR login).
$principal = New-ScheduledTaskPrincipal `
    -UserId (whoami) `
    -LogonType Interactive `
    -RunLevel Limited

# Replace if already registered
if (Get-ScheduledTask -TaskName 'VoucherAudit_DailyRun' -ErrorAction SilentlyContinue) {
    Write-Host 'Existing task found - replacing.'
    Unregister-ScheduledTask -TaskName 'VoucherAudit_DailyRun' -Confirm:$false
}

Register-ScheduledTask `
    -TaskName 'VoucherAudit_DailyRun' `
    -Description 'Rite Water Voucher Audit Skill: pulls in-process claims from SpineHR at 12:00 PM and generates audit reports.' `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal | Out-Null

Write-Host ''
Write-Host 'Task "VoucherAudit_DailyRun" registered successfully.'
Write-Host 'It will run every day at 12:00 PM while the PC is on.'
Write-Host ''
Write-Host 'To run it now manually:'
Write-Host '    Start-ScheduledTask -TaskName VoucherAudit_DailyRun'
Write-Host ''
Write-Host 'To watch the log live:'
Write-Host '    Get-Content -Wait history\daily_runs\run_$(Get-Date -Format yyyy-MM-dd).log'
