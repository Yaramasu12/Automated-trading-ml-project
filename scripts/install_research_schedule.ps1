# Registers scripts/scheduled_research_session.py as a recurring Windows
# Scheduled Task -- Phase 3 of the strategy-catalog-reconnection work
# (2026-09-08): the continuous LLM hypothesis-research loop, so edge search
# keeps happening on a schedule instead of only when someone remembers to
# run it by hand. Mirrors AgentSai\install\register_all.ps1's own
# New-ScheduledTaskTrigger pattern (that repo's own proven approach for
# "runs unattended, survives logoff, no admin rights needed").
#
# Cadence: nightly, after market close and well clear of the 15:40 IST
# daily-review job (scripts/daily_scheduler.py) and any EOD square-off --
# this reads only cached historical CSVs, it does not touch live capital or
# the broker session, so it has no hard ordering dependency on those, but
# running well after them avoids resource contention with the live loop
# during market hours.
#
# Undo:
#   Unregister-ScheduledTask -TaskName "TradingResearchLoop" -Confirm:$false

$ErrorActionPreference = "Continue"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python   = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) { throw "venv not found at $Python" }

$action = New-ScheduledTaskAction -Execute $Python `
    -Argument "-m scripts.scheduled_research_session" `
    -WorkingDirectory $RepoRoot

# Once daily at 22:00 local time -- well after the 15:40 IST daily review
# and any EOD activity, and before the next trading day's pre-market.
$trigger = New-ScheduledTaskTrigger -Daily -At 22:00

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3) -MultipleInstances IgnoreNew

try {
    Register-ScheduledTask -TaskName "TradingResearchLoop" -Action $action -Trigger $trigger `
        -Settings $settings -Description "Nightly LLM hypothesis-research loop (scripts/scheduled_research_session.py) - searches for new validated trading edge; see needs_review.log for anything that survives." `
        -Force -ErrorAction Stop | Out-Null
    Write-Host "OK TradingResearchLoop registered - runs nightly at 22:00 local time"
} catch {
    Write-Host "FAIL TradingResearchLoop : $($_.Exception.Message)"
}
