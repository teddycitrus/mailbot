# Registers every scheduled job. Run once from the project root:
#   powershell -ExecutionPolicy Bypass -File scripts\install_schedule.ps1
#
# Four jobs, deliberately separated:
#   mailbot-prep    weekday 08:25              scan mailbox, render drafts
#   mailbot-send    weekday 08:30-13:30 /15m   send one message per tick
#   mailbot-build   every day 19:30            slow, discover + enrich
#   mailbot-digest  Friday  17:00              weekly summary email
#
# Triggers fire on this machine's local clock. The bot separately refuses to
# send outside SEND_WINDOW_START..SEND_WINDOW_END in the recipient's own zone,
# so a tick that fires when nobody's window is open simply sends nothing.

$root = Split-Path -Parent $PSScriptRoot
$weekdays = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
# Mining is send-free: nightly_build.ps1 only runs hn/discover/gh/enrich,
# so it is safe at the weekend. Building Saturday and Sunday means Monday
# opens on three nights of accumulated stock rather than one, and Monday is
# the day the queue most needs to be deep. Sending stays weekday-only,
# enforced both by the triggers below and by the weekend check in
# src/emailer.py, so a weekend build can never turn into a weekend send.
$alldays = $weekdays + @("Saturday", "Sunday")

# Register-ScheduledTask defaults to an Interactive principal, which ties every
# job to a live console session: log off, or let the session end, and the job is
# killed mid-run with 0xC000013A. S4U runs the task whether or not the user is
# logged on and needs no stored password, but setting it requires elevation.
$script:elevated = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
$script:principal = $null
if ($script:elevated) {
    $script:principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME `
        -LogonType S4U -RunLevel Limited
}

function Install-MailbotTask {
    param(
        $Name,
        $Script,
        $At,
        $Description,
        $Arguments = "",
        $DaysOfWeek = $null,
        [int]$RepeatEveryMinutes = 0,
        [double]$RepeatForHours = 0,
        [int]$TimeLimitMinutes = 120,
        [int]$RestartCount = 0,
        [int]$RestartMinutes = 5
    )

    $path = Join-Path $root $Script
    if (-not (Test-Path $path)) { throw "cannot find $path" }
    if ($null -eq $DaysOfWeek) { $DaysOfWeek = $weekdays }

    $argLine = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$path`""
    if ($Arguments) { $argLine += " $Arguments" }

    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument $argLine -WorkingDirectory $root

    $triggers = @()
    foreach ($t in @($At)) {
        $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $DaysOfWeek -At $t
        if ($RepeatEveryMinutes -gt 0) {
            # A weekly trigger has no repetition parameters of its own, so borrow
            # the Repetition object from a throwaway one-off trigger. This is the
            # only way to get "every N minutes for H hours" onto a weekly trigger.
            $trigger.Repetition = (New-ScheduledTaskTrigger -Once -At $t `
                -RepetitionInterval (New-TimeSpan -Minutes $RepeatEveryMinutes) `
                -RepetitionDuration (New-TimeSpan -Hours $RepeatForHours)).Repetition
        }
        $triggers += $trigger
    }

    # WakeToRun is set here, but it is inert unless the machine's power scheme
    # also allows wake timers:
    #   powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1
    #   powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1
    #   powercfg /setactive SCHEME_CURRENT
    $settingArgs = @{
        StartWhenAvailable         = $true
        WakeToRun                  = $true
        DontStopIfGoingOnBatteries = $true
        AllowStartIfOnBatteries    = $true
        ExecutionTimeLimit         = (New-TimeSpan -Minutes $TimeLimitMinutes)
    }
    if ($RestartCount -gt 0) {
        $settingArgs.RestartCount    = $RestartCount
        $settingArgs.RestartInterval = (New-TimeSpan -Minutes $RestartMinutes)
    }
    $settings = New-ScheduledTaskSettingsSet @settingArgs

    if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
    }
    if ($script:principal) {
        Register-ScheduledTask -TaskName $Name -Action $action -Trigger $triggers `
            -Settings $settings -Description $Description `
            -Principal $script:principal | Out-Null
    } else {
        Register-ScheduledTask -TaskName $Name -Action $action -Trigger $triggers `
            -Settings $settings -Description $Description | Out-Null
    }

    $when = $At -join ', '
    if ($RepeatEveryMinutes -gt 0) {
        $when += " then every ${RepeatEveryMinutes}m for ${RepeatForHours}h"
    }
    Write-Host "registered '$Name' at $when"
}

# Render the day's drafts just before the first window opens. This one runs
# once a day, so a single failed launch costs every draft for that day and
# there is no later trigger to make it up; the ticks need no such retry
# because another one follows 15 minutes behind.
Install-MailbotTask -Name "mailbot-prep" -Script "scripts\daily_run.ps1" `
    -Arguments "-Mode prep" -At "8:25am" -TimeLimitMinutes 30 `
    -RestartCount 2 -RestartMinutes 5 `
    -Description "Mailbot: scan mailbox and render drafts"

# One tick every 15 minutes from 08:30 to 13:30, each sending at most one
# message. 08:30-10:30 covers Eastern recipients in their own morning and
# 11:30-13:30 covers Pacific ones; the hour between belongs to nobody and its
# ticks no-op. Spreading the cap this way means the day's sends arrive across
# each recipient's 08:30-10:30 rather than all inside four minutes.
Install-MailbotTask -Name "mailbot-send" -Script "scripts\daily_run.ps1" `
    -Arguments "-Mode tick" -At "8:30am" `
    -RepeatEveryMinutes 15 -RepeatForHours 5 -TimeLimitMinutes 10 `
    -Description "Mailbot: send queued outreach, paced"

Install-MailbotTask -Name "mailbot-build" -Script "scripts\nightly_build.ps1" `
    -At "7:30pm" -DaysOfWeek $alldays `
    -Description "Mailbot: discover and enrich new companies"

Install-MailbotTask -Name "mailbot-digest" -Script "scripts\weekly_digest.ps1" `
    -At "5:00pm" -DaysOfWeek @("Friday") `
    -Description "Mailbot: weekly summary email"

# The old single task is superseded by the pair above.
if (Get-ScheduledTask -TaskName "mailbot-daily" -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName "mailbot-daily" -Confirm:$false
    Write-Host "removed superseded 'mailbot-daily'"
}

Write-Host ""
Write-Host "check:  Get-ScheduledTask -TaskName mailbot-*"
Write-Host "run now: Start-ScheduledTask -TaskName mailbot-build"
Write-Host "remove:  Unregister-ScheduledTask -TaskName mailbot-send -Confirm:`$false"

if (-not $script:elevated) {
    Write-Host ""
    Write-Host "WARNING: not elevated, so the jobs are registered to run only" `
        -ForegroundColor Yellow
    Write-Host "         while '$env:USERNAME' is logged on. They will be killed" `
        -ForegroundColor Yellow
    Write-Host "         if the session ends. Re-run this script from an" `
        -ForegroundColor Yellow
    Write-Host "         Administrator PowerShell to fix that." -ForegroundColor Yellow
}
