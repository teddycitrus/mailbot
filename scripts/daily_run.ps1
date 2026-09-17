# Morning job: send drafts that were already written, hours or days ago.
#
# This script does no discovery, no enrichment and, since 2026-09-17, no
# rendering either. All of those are slow or depend on a third party, and
# anything slow on this path burns the send window it is standing in.
# Drafting moved to draft_run.ps1, which runs all day and has no deadline;
# see the note at the top of that file for why. What is left here is the one
# job that genuinely has to happen at a particular hour: sending.
#
# Two modes, because the two halves have very different costs:
#
#   prep   once at 08:25. A full mailbox scan, which is the slow half: every
#          examined message is a full body fetch, so it must not run on every
#          tick. Renders nothing.
#   tick   every 15 minutes from 08:30 to 13:30. Sends at most two messages,
#          so the day's cap dribbles out across each recipient's own morning
#          instead of landing as one burst four minutes wide.
#
# The bot itself decides who is eligible on any given tick: with
# PER_RECIPIENT_TIMEZONE on, a draft only sends inside the 08:30-10:30 window
# where its recipient sits. Ticks outside every open window simply send nothing.

param(
    [ValidateSet("prep", "tick")]
    [string]$Mode = "tick"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$logDir = Join-Path $root "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$script:log = Join-Path $logDir ("send-" + (Get-Date -Format "yyyy-MM-dd") + ".log")

function Write-Log {
    # prep (08:25) and the first tick (08:30) are separate scheduled tasks
    # writing to this one file, and StartWhenAvailable fires both at the same
    # instant when the machine wakes to a morning it slept through. Out-File
    # takes an exclusive handle, so the loser threw on its very first write,
    # and with ErrorActionPreference = Stop that ended the run before a single
    # line reached the log: task result 0x1 with nothing logged to explain it,
    # which is exactly what prep did on 2026-09-11.
    #
    # Logging must never be the thing that kills a run, so retry a locked
    # file and then give up quietly rather than throwing.
    param([string]$Text)
    for ($attempt = 0; $attempt -lt 10; $attempt++) {
        try {
            $Text | Out-File -Append -Encoding utf8 $script:log -ErrorAction Stop
            return
        }
        catch {
            Start-Sleep -Milliseconds (150 * ($attempt + 1) + (Get-Random -Maximum 150))
        }
    }
    Write-Host "mailbot: gave up writing to $script:log"
}

function Write-Mailbot {
    # PowerShell's *>> redirection writes UTF-16, which most tools render as
    # mojibake. Capture every stream and write plain UTF-8 instead.
    param([Parameter(ValueFromRemainingArguments = $true)]$MailbotArgs)
    $out = (& $script:python -m src.main @MailbotArgs *>&1) | Out-String
    $code = $LASTEXITCODE
    Write-Log $out.TrimEnd()
    # Remember the worst code any step returned, so the exit at the bottom can
    # tell the scheduler the truth. Without it the script's own exit code was
    # whatever powershell.exe felt like, and prep reported task result 0x1 on
    # 2026-09-14 after a run whose every step had in fact succeeded -- which
    # then burned both of its RestartCount attempts re-running a clean pass.
    if ($code -and $code -gt $script:worst) { $script:worst = $code }
    # The footer reports $LASTEXITCODE, and the retry loop above runs after
    # the python call, so put it back rather than trusting it to survive.
    $global:LASTEXITCODE = $code
}

$script:worst = 0
$script:python = "python"
$venv = Join-Path $root ".venv\Scripts\python.exe"
if (Test-Path $venv) { $script:python = $venv }

Write-Log "=== $Mode run $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ==="

# Bounces and opt-outs first, on every tick and not just at prep: someone who
# replies "stop" at 09:00 must not be mailed by the 09:15 tick.
#
# A tick only needs the mail that landed since the previous tick, and every
# examined message is a full body fetch. Scanning the default 30 days on each
# of twenty ticks costs about ninety seconds a time, which is enough to push a
# send past the end of the window it was meant to land in.
if ($Mode -eq "prep") {
    Write-Mailbot inbox
}
else {
    Write-Mailbot inbox --days 1 --limit 40

    # Before sending, not after: this is what notices a draft sent by hand
    # from the phone, marks it sent here, and takes it out of the queue the
    # next line is about to send from. It also clears the Gmail copy of
    # anything the previous tick sent. Two days of Sent is plenty at this
    # cadence and keeps the fetch small.
    Write-Mailbot mirror --limit 25 --days 2

    # Two messages per tick. The tick count, not the cap, used to be what ended
    # the day: ticks run 08:30-13:30, but a recipient is only eligible during
    # the 08:30-10:30 window in their own zone, so each city gets about nine
    # usable ticks. At one send per tick that ceiling is roughly 18 a day, which
    # silently caps DAILY_SEND_LIMIT below whatever it is set to. Two per tick
    # puts the cap back in charge. SEND_DELAY_SECONDS still paces them apart.
    Write-Mailbot send --limit 2

    # One nudge to anyone who never answered. Shares the daily cap with the
    # sends above, so the two together can never exceed DAILY_SEND_LIMIT.
    Write-Mailbot followup --limit 1
}

Write-Log "=== finished $(Get-Date -Format 'HH:mm:ss') exit=$script:worst ==="
exit $script:worst
