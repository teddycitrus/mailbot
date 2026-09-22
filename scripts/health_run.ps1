# Daily health check. Runs before the send window opens, so a credential that
# died overnight is known about while there is still a morning left to fix it.
#
# The check has to assume the thing it is reporting on may be what is broken,
# so a failure is announced three ways and each is independent of the others:
#
#   email    mailbot health mails the report to the operator. Useless exactly
#            when SMTP is the fault, which is why it is not the only channel.
#   toast    a desktop notification raised here, from the exit code. Survives
#            anything wrong with mail, and lands in Action Center if the
#            session is locked.
#   file     logs\ALERT.txt, rewritten by every run. Always on disk, whatever
#            else failed.
#
# Exit code is non-zero when something is broken, so Task Scheduler's own Last
# Run Result column shows it too.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$logDir = Join-Path $root "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$script:log = Join-Path $logDir ("health-" + (Get-Date -Format "yyyy-MM-dd") + ".log")

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
    # WinRT toast, no third-party module. Borrowing PowerShell's own AppUserModelID
    # because a notification has to come from something already registered with
    # the shell, and registering our own would be a setup step that could itself
    # fail silently.
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

$script:python = "python"
$venv = Join-Path $root ".venv\Scripts\python.exe"
if (Test-Path $venv) { $script:python = $venv }

Write-Log "=== health run $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ==="

$out = (& $script:python -m src.main health *>&1) | Out-String
$code = $LASTEXITCODE
Write-Log $out.TrimEnd()
Write-Host $out.TrimEnd()

if ($code -ne 0) {
    # First line of the alert file names what is broken; the toast shows it so
    # the notification says something useful without being opened.
    $summary = "Something is broken. See logs\ALERT.txt"
    $alert = Join-Path $root "logs\ALERT.txt"
    if (Test-Path $alert) {
        $broken = Get-Content $alert | Where-Object { $_ -match "^  \S+:" } | Select-Object -First 2
        if ($broken) { $summary = ($broken -join "; ").Trim() }
    }
    Show-Toast -Title "Mailbot is broken" -Text $summary
    Write-Log "=== finished $(Get-Date -Format 'HH:mm:ss') PROBLEMS exit=$code ==="
}
else {
    Write-Log "=== finished $(Get-Date -Format 'HH:mm:ss') all good ==="
}

exit $code
