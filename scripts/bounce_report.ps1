# End-of-window bounce check. Runs after the last send tick of the day.
#
# The warmup ramp already brakes on the bounce rate, but it reads the last
# hundred sends. That is the right horizon for sender reputation and the wrong
# one for noticing that today went wrong: two bounces in twenty-one sends is
# 9.5% for the day and barely moves a hundred-send average. This looks at one
# day on its own and mails you when it crosses DAILY_BOUNCE_ALERT_PCT.
#
# The mailbox is scanned first. A failure notice that has not been read yet is
# not in the database, and a check that runs before the scan would report a
# clean day every time.
#
# Weekdays only, because sending is weekday only. 13:45 is fifteen minutes
# after the last tick of the 08:30-13:30 window.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$logDir = Join-Path $root "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$script:log = Join-Path $logDir ("bounce-" + (Get-Date -Format "yyyy-MM-dd") + ".log")

function Write-Log {
    # Same retry as the other jobs: two tasks can wake to the same instant and
    # Out-File takes an exclusive handle. Logging must never kill a run.
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

function Show-Toast {
    # Borrowing PowerShell's own AppUserModelID, as health_run.ps1 does: a
    # notification has to come from something already registered with the shell.
    param([string]$Title, [string]$Text)
    $appId = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
    try {
        [void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
        $xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
            [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
        $lines = $xml.GetElementsByTagName("text")
        [void]$lines.Item(0).AppendChild($xml.CreateTextNode($Title))
        [void]$lines.Item(1).AppendChild($xml.CreateTextNode($Text))
        $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
        [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
    }
    catch {
        Write-Log "toast notification failed: $_"
    }
}

function Write-Mailbot {
    # PowerShell's *>> redirection writes UTF-16, which most tools render as
    # mojibake. Capture every stream and write plain UTF-8 instead.
    param([Parameter(ValueFromRemainingArguments = $true)]$MailbotArgs)
    $out = (& $script:python -m src.main @MailbotArgs *>&1) | Out-String
    $script:code = $LASTEXITCODE
    Write-Log $out.TrimEnd()
    Write-Host $out.TrimEnd()
}

$script:python = "python"
$venv = Join-Path $root ".venv\Scripts\python.exe"
if (Test-Path $venv) { $script:python = $venv }

Write-Log "=== bounce check $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ==="

# Read the mailbox before judging it. One day back is plenty: every message
# being judged went out inside the window that closed fifteen minutes ago, and
# a bounce notice normally arrives within seconds.
Write-Mailbot inbox --days 1 --limit 80

Write-Mailbot bounce-report
$rate = $script:code

if ($rate -ne 0) {
    $summary = "Today's bounce rate is over the threshold. See logs\ALERT.txt"
    $alert = Join-Path $root "logs\ALERT.txt"
    if (Test-Path $alert) {
        $first = (Get-Content $alert -TotalCount 1)
        if ($first) { $summary = $first.Trim() }
    }
    Show-Toast -Title "Mailbot: high bounce rate" -Text $summary
    Write-Log "=== finished $(Get-Date -Format 'HH:mm:ss') OVER THRESHOLD ==="
}
else {
    Write-Log "=== finished $(Get-Date -Format 'HH:mm:ss') rate within limits ==="
}

exit $rate
