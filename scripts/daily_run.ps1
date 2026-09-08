# Morning job: send from the queue the nightly build already prepared.
#
# Deliberately does no discovery or enrichment. Those involve slow third-party
# network calls, and a single hung scrape would otherwise burn the 08:30-10:00
# send window. This script only touches the local queue plus SMTP and IMAP, so
# it finishes in minutes.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$logDir = Join-Path $root "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$script:log = Join-Path $logDir ("send-" + (Get-Date -Format "yyyy-MM-dd") + ".log")

function Write-Mailbot {
    # PowerShell's *>> redirection writes UTF-16, which most tools render as
    # mojibake. Capture every stream and write plain UTF-8 instead.
    param([Parameter(ValueFromRemainingArguments = $true)]$MailbotArgs)
    (& $script:python -m src.main @MailbotArgs *>&1) |
        Out-File -Append -Encoding utf8 $script:log
}

$script:python = "python"
$venv = Join-Path $root ".venv\Scripts\python.exe"
if (Test-Path $venv) { $script:python = $venv }

"=== send run $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File -Append -Encoding utf8 $script:log

# Bounces and opt-outs first: never mail someone who asked to be left alone,
# even if their draft was queued before they replied.
Write-Mailbot inbox
Write-Mailbot queue --limit 25
Write-Mailbot send --limit 25

# One nudge to anyone who never answered. Shares the daily cap with the
# sends above, so the two together can never exceed DAILY_SEND_LIMIT.
Write-Mailbot followup --limit 10

"=== finished $(Get-Date -Format 'HH:mm:ss') exit=$LASTEXITCODE ===" | Out-File -Append -Encoding utf8 $script:log
