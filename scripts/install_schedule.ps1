# Registers both scheduled jobs. Run once from the project root:
#   powershell -ExecutionPolicy Bypass -File scripts\install_schedule.ps1
#
# Two jobs, deliberately separated:
#   mailbot-send   weekday 08:35  fast, queue + send + inbox
#   mailbot-build  weekday 19:30  slow, discover + enrich
#
# Triggers fire on this machine's local clock. The bot separately refuses to
# send outside SEND_WINDOW_START..SEND_WINDOW_END in SEND_TIMEZONE, so if this
# machine is not on Toronto time, adjust the send trigger to match.

$root = Split-Path -Parent $PSScriptRoot
$weekdays = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")

function Install-MailbotTask {
    param($Name, $Script, $At, $Description)
    # $At may be a single time or several; each becomes its own trigger.

    $path = Join-Path $root $Script
    if (-not (Test-Path $path)) { throw "cannot find $path" }

    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$path`"" `
        -WorkingDirectory $root
    $triggers = @()
    foreach ($t in @($At)) {
        $triggers += New-ScheduledTaskTrigger -Weekly -DaysOfWeek $weekdays -At $t
    }
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Hours 2)

    if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
    }
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $triggers `
        -Settings $settings -Description $Description | Out-Null
    Write-Host "registered '$Name' weekdays at $($At -join ', ')"
}

# Two firings: 08:35 catches Eastern recipients in their own morning, 11:35
# catches Pacific ones (08:35 local). Each run only sends to people whose local
# window is currently open, so the two do not overlap.
Install-MailbotTask -Name "mailbot-send" -Script "scripts\daily_run.ps1" `
    -At @("8:35am", "11:35am") -Description "Mailbot: send queued outreach"
Install-MailbotTask -Name "mailbot-build" -Script "scripts\nightly_build.ps1" `
    -At "7:30pm" -Description "Mailbot: discover and enrich new companies"

$weekdaysAll = $weekdays
$weekdays = @("Friday")
Install-MailbotTask -Name "mailbot-digest" -Script "scripts\weekly_digest.ps1" `
    -At "5:00pm" -Description "Mailbot: weekly summary email"
$weekdays = $weekdaysAll

# The old single task is superseded by the pair above.
if (Get-ScheduledTask -TaskName "mailbot-daily" -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName "mailbot-daily" -Confirm:$false
    Write-Host "removed superseded 'mailbot-daily'"
}

Write-Host ""
Write-Host "check:  Get-ScheduledTask -TaskName mailbot-*"
Write-Host "run now: Start-ScheduledTask -TaskName mailbot-build"
Write-Host "remove:  Unregister-ScheduledTask -TaskName mailbot-send -Confirm:`$false"
