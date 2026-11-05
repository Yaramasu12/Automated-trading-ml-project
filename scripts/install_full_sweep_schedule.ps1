# Registers scripts/scheduled_full_sweep.py as a recurring Windows Scheduled
# Task -- the continuous multi-asset edge sweep (2026-11-04). Complements the
# existing TradingResearchLoop (nightly LLM hypothesis loop, 5 instruments):
# this re-screens EVERY asset class (stocks, MCX, index-futures basis, intraday,
# gap, FX) so a newly-emerging edge is caught between manual sweeps. Any screen
# candidate is appended to data/research_sessions/needs_review.log for a human
# to validate (full gates + realistic execution) -- it never promotes or trades.
#
# Mirrors scripts/install_research_schedule.ps1's proven pattern (runs
# unattended, survives logoff, no admin rights needed).
#
# Cadence: WEEKLY, Sunday 20:00 local -- markets closed, well clear of the
# nightly 22:00 research loop and all daily/EOD jobs. Weekly (not nightly)
# because the sweep fetches a lot of candle data and edges do not change daily.
#
# Undo:
#   Unregister-ScheduledTask -TaskName "TradingFullSweep" -Confirm:$false

$ErrorActionPreference = "Continue"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python   = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) { throw "venv not found at $Python" }

$action = New-ScheduledTaskAction -Execute $Python `
    -Argument "-m scripts.scheduled_full_sweep" `
    -WorkingDirectory $RepoRoot

$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 20:00

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew

try {
    Register-ScheduledTask -TaskName "TradingFullSweep" -Action $action -Trigger $trigger `
        -Settings $settings -Description "Weekly multi-asset edge re-screen (scripts/scheduled_full_sweep.py) - escalates screen candidates to data/research_sessions/needs_review.log for validation; never trades." `
        -Force -ErrorAction Stop | Out-Null
    Write-Host "OK TradingFullSweep registered - runs weekly Sunday 20:00 local time"
} catch {
    Write-Host "FAILED to register TradingFullSweep: $_"
    exit 1
}
