# Drafting job: keep a deep queue of finished drafts at all times.
#
# Split out of the morning run deliberately. Rendering used to happen at 08:25
# on weekdays, which made a whole day's sending depend on this machine being
# awake for that one minute. Often it was not. On 2026-09-17 the laptop slept
# from the previous evening until 10:25, so prep never ran, the first tick
# fired at 10:45, and by then the Eastern window had closed.
#
# So drafting no longer has an appointment to keep. It runs every two hours,
# seven days a week, catches up on wake, and the morning job only sends what
# is already waiting.
#
# Cheap by design. queue stops as soon as QUEUE_TARGET drafts are waiting, so
# a run that finds a full queue makes no LLM calls at all and costs a few
# seconds. That is what makes running it all day affordable.
#
# The mirror pass then copies every queued draft into the Gmail Drafts folder.
# That is the real fallback: a day where this machine never wakes can still be
# sent by hand from a phone, and a draft sent either way is never sent twice.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$logDir = Join-Path $root "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$script:log = Join-Path $logDir ("draft-" + (Get-Date -Format "yyyy-MM-dd") + ".log")

function Write-Log {
    # Same retry as daily_run.ps1: two jobs can wake to the same instant and
    # Out-File takes an exclusive handle. Logging must never be the thing that
    # kills a run.
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
    $global:LASTEXITCODE = $code
}

$script:python = "python"
$venv = Join-Path $root ".venv\Scripts\python.exe"
if (Test-Path $venv) { $script:python = $venv }

Write-Log "=== draft run $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ==="

# No --target, so QUEUE_TARGET decides. The limit caps one run's LLM spend;
# a queue that starts empty fills over a few runs rather than in one burst.
Write-Mailbot queue --limit 25

# Push the copies, clear the copies of anything already sent, and pick up
# anything sent by hand since the last pass.
Write-Mailbot mirror --limit 50

Write-Log "=== finished $(Get-Date -Format 'HH:mm:ss') exit=$LASTEXITCODE ==="
