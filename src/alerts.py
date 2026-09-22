"""The daily "is this still working?" check, and how you find out when it is not.

The whole system fails silently. An app password is revoked, or the ISP starts
blocking port 25, and nothing raises: sends just stop, or verification quietly
degrades and every company starts looking unreachable. The weekly digest that
would have mentioned it goes out over the same SMTP that just died, so the one
report that could tell you is the one that cannot.

So this runs on its own, every morning, before the send window opens, and it is
built on the assumption that the thing it is reporting on may be the thing that
is broken:

  email      the normal channel, sent to the operator's own address
  exit code  non-zero on any failure, which is what the scheduled task shows
             and what the wrapper script turns into a desktop notification
  file       logs/ALERT.txt, rewritten each run, so there is always something
             on disk that says what was wrong and when

One alert per distinct problem per day. A fault that persists for a week should
not produce seven identical mails, but a new fault appearing on top of an old
one should still get through.
"""

from __future__ import annotations

import hashlib
import smtplib
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .database import Database
from .doctor import FAIL, WARN, Check, run_checks
from .emailer import build_message, local_date, make_backend

ALERT_FILE = "logs/ALERT.txt"
ALERT_EVENT = "health_alert"
BOUNCE_EVENT = "bounce_alert"


@dataclass
class HealthReport:
    checks: list[Check] = field(default_factory=list)
    failures: list[Check] = field(default_factory=list)
    warnings: list[Check] = field(default_factory=list)
    emailed: bool = False
    email_error: str = ""
    suppressed: bool = False     # already alerted about exactly this today

    @property
    def healthy(self) -> bool:
        return not self.failures

    def fingerprint(self) -> str:
        """Identifies the set of things wrong, so a repeat is recognisable."""
        blob = "|".join(sorted(f"{c.name}:{c.detail[:60]}" for c in self.failures))
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _body(report: HealthReport, settings: Settings, today: str) -> str:
    lines = [
        f"Mailbot health check, {today}",
        "",
        f"{len(report.failures)} failing, {len(report.warnings)} warning, "
        f"{len(report.checks)} checked.",
        "",
    ]
    if report.failures:
        lines.append("BROKEN -- these stop mail going out or wreck the funnel:")
        lines += [f"  {c.name}: {c.detail}" for c in report.failures]
        lines.append("")
    if report.warnings:
        lines.append("Worth a look:")
        lines += [f"  {c.name}: {c.detail}" for c in report.warnings]
        lines.append("")
    lines += [
        "Everything checked:",
        *[f"  {c.state:5s} {c.name}" for c in report.checks],
        "",
        f"Run 'python -m src.main doctor' on {settings.db_path.parent} for detail.",
        "",
    ]
    return "\n".join(lines)


def _write_alert_file(settings: Settings, text: str) -> Path:
    path = settings.db_path.parent / ALERT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _already_alerted(db: Database, fingerprint: str, today: str) -> bool:
    row = db.conn.execute(
        """SELECT 1 FROM events WHERE kind = ? AND ref = ?
           AND substr(created_at, 1, 10) = ?""",
        (ALERT_EVENT, fingerprint, today),
    ).fetchone()
    return row is not None


def _email(settings: Settings, subject: str, body: str) -> tuple[bool, str]:
    """Mail the operator. Failing here is expected when SMTP is the fault."""
    try:
        message = build_message(
            subject, body, settings.from_email, settings.from_name,
            settings.from_email, settings.from_name,
        )
        with make_backend(settings) as backend:
            result = backend.send(message)
        return result.ok, result.error
    except (smtplib.SMTPException, OSError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


def health_check(settings: Settings, notify: bool = True,
                 checks: list[Check] | None = None) -> HealthReport:
    """Run every check, then make sure a failure is impossible to miss."""
    report = HealthReport(checks=checks if checks is not None else run_checks(settings))
    report.failures = [c for c in report.checks if c.state == FAIL]
    report.warnings = [c for c in report.checks if c.state == WARN]

    today = local_date(settings.send_timezone)
    body = _body(report, settings, today)

    if report.healthy:
        # Clear the file rather than leaving yesterday's panic sitting there
        # looking current.
        _write_alert_file(settings, f"Mailbot health check, {today}: all good.\n")
        return report

    _write_alert_file(settings, body)
    if not notify:
        return report

    names = ", ".join(c.name for c in report.failures[:3])
    subject = f"Mailbot BROKEN: {names}" + (
        f" and {len(report.failures) - 3} more" if len(report.failures) > 3 else "")

    fingerprint = report.fingerprint()
    try:
        db = Database(settings.db_path)
    except Exception:
        db = None
    try:
        if db is not None and _already_alerted(db, fingerprint, today):
            report.suppressed = True
            return report
        report.emailed, report.email_error = _email(settings, subject, body)
        if db is not None and report.emailed:
            db.log(ALERT_EVENT, fingerprint, names[:120])
    finally:
        if db is not None:
            db.close()
    return report


# --------------------------------------------------- one day's bounce rate

@dataclass
class BounceReport:
    day: str = ""
    sent: int = 0
    bounced: list = field(default_factory=list)
    threshold: int = 0
    emailed: bool = False
    email_error: str = ""
    too_few: bool = False

    @property
    def pct(self) -> float:
        return (100.0 * len(self.bounced) / self.sent) if self.sent else 0.0

    @property
    def over(self) -> bool:
        return bool(self.bounced) and not self.too_few and self.pct > self.threshold


def _bounce_body(report: BounceReport, settings: Settings) -> str:
    lines = [
        f"Mailbot: {report.pct:.0f}% of today's mail bounced",
        "",
        f"  date      {report.day}",
        f"  sent      {report.sent}",
        f"  bounced   {len(report.bounced)}",
        f"  rate      {report.pct:.1f}%   (alerting above "
        f"{report.threshold}%)",
        "",
        "Bounced:",
    ]
    for row in report.bounced:
        lines.append(f"  {row['email']}  ({row['company_name'] or '?'})")
        lines.append(f"      confidence {row['confidence']}, "
                     f"{row['verify_status']}: {row['verify_detail'][:70]}")
    lines += [
        "",
        "The confidence and verdict above are what the address was believed to",
        "be worth when it was drafted. If they read 'no SMTP answer' then the",
        "address was never actually proved and the pool needs another pass:",
        "",
        "    python -m src.main reverify",
        "",
        "Bounces are attributed to the day the message went out. A notice that",
        "arrives after this check runs lands in tomorrow's count instead.",
        "",
    ]
    return "\n".join(lines)


def bounce_check(settings: Settings, db: Database, day: str = "",
                 notify: bool = True) -> BounceReport:
    """Mail the operator when a single day's bounce rate goes over the line.

    Separate from the warmup ramp's own brake on purpose. That one reads the
    last hundred sends, which is the right horizon for sender reputation but
    cannot see a single bad day: two bounces in twenty-one sends is 9.5% for
    the day and moves a hundred-send average by two points. A day that goes
    wrong should be something you hear about the same afternoon.
    """
    day = day or local_date(settings.send_timezone)
    sent, bounced = db.bounces_on(day)
    report = BounceReport(
        day=day, sent=sent, bounced=list(bounced),
        threshold=settings.daily_bounce_alert_pct,
        # One bounce out of two sends is 50% and means nothing. The floor
        # keeps a quiet day from reading as a catastrophe.
        too_few=sent < settings.daily_bounce_alert_min_sends,
    )
    if not report.over or not notify:
        return report

    subject = (f"Mailbot: {report.pct:.0f}% bounce rate today "
               f"({len(report.bounced)}/{report.sent})")
    body = _bounce_body(report, settings)
    report.emailed, report.email_error = _email(settings, subject, body)
    _write_alert_file(settings, body)
    if report.emailed:
        db.log(BOUNCE_EVENT, day, f"{report.pct:.1f}% "
               f"({len(report.bounced)}/{report.sent})")
    return report


def print_bounce_report(report: BounceReport) -> None:
    rate = f"{report.pct:.1f}%"
    print(f"bounce check {report.day}: {len(report.bounced)}/{report.sent} "
          f"bounced = {rate} (alerting above {report.threshold}%)")
    for row in report.bounced:
        print(f"  {row['email']}  conf {row['confidence']}  "
              f"{row['verify_detail'][:52]}")
    if report.too_few:
        print(f"under the {report.sent}-send floor, not judging a day this small")
    elif not report.over:
        print("under the threshold, no alert sent")
    elif report.emailed:
        print("OVER THRESHOLD, alert email sent")
    else:
        print(f"OVER THRESHOLD, but the email failed ({report.email_error})")
        print("see logs/ALERT.txt")


def print_report(report: HealthReport) -> None:
    from .reporting import _print_table
    _print_table([(c.name, c.state, c.detail) for c in report.checks],
                 ("check", "state", "detail"))
    print()
    if report.healthy:
        print("all good")
        return
    print(f"{len(report.failures)} problem(s)")
    if report.suppressed:
        print("alert email: already sent for this exact fault today")
    elif report.emailed:
        print("alert email: sent")
    else:
        print(f"alert email: COULD NOT SEND ({report.email_error or 'no attempt'})")
        print("see logs/ALERT.txt")
