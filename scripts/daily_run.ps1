# Morning job: send from the queue the nightly build already prepared.
#
# Deliberately does no discovery or enrichment. Those involve slow third-party
# network calls, and a single hung scrape would otherwise burn the send window.
# This script only touches the local queue plus SMTP and IMAP, so it finishes
# in seconds.
#
# Two modes, because the two halves have very different costs:
#
#   prep   once at 08:25. Scans the mailbox and renders drafts. Rendering calls
#          Groq, so it must not run on every tick or it burns the LLM budget.
#   tick   every 15 minutes from 08:30 to 13:30. Sends at most one message, so
#          the day's cap dribbles out across each recipient's own morning
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
    # The footer reports $LASTEXITCODE, and the retry loop above runs after
    # the python call, so put it back rather than trusting it to survive.
    $global:LASTEXITCODE = $code
}

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
    # Rendering is the expensive half. Once a day is enough.
    Write-Mailbot queue --limit 25
}
else {
    Write-Mailbot inbox --days 1 --limit 40

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

Write-Log "=== finished $(Get-Date -Format 'HH:mm:ss') exit=$LASTEXITCODE ==="
