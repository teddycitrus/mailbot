# Nightly job: refill the contact pool so the morning send always has stock.
#
# Runs in the evening because it is slow (SMTP probes and site scrapes, roughly
# a minute per company) and nothing about it is time critical. Sending is never
# blocked waiting on it.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$logDir = Join-Path $root "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$script:log = Join-Path $logDir ("build-" + (Get-Date -Format "yyyy-MM-dd") + ".log")

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

"=== build run $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File -Append -Encoding utf8 $script:log
# HN first: those contacts arrive already published, no inference needed.
Write-Mailbot hn --limit 40
Write-Mailbot discover --limit 60
Write-Mailbot enrich   --limit 60
Write-Mailbot stats
"=== finished $(Get-Date -Format 'HH:mm:ss') exit=$LASTEXITCODE ===" | Out-File -Append -Encoding utf8 $script:log
