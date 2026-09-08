# Weekly summary mailed to the operator. Read-only apart from sending itself.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$logDir = Join-Path $root "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$script:log = Join-Path $logDir ("digest-" + (Get-Date -Format "yyyy-MM-dd") + ".log")

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

Write-Mailbot digest --days 7
