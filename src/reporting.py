"""Reporting: the weekly digest email and the on-screen summary.

Separated from the pipeline so neither file outgrows its purpose. Both are
read-only over the database apart from the digest actually mailing itself.
"""

from __future__ import annotations

import smtplib
from datetime import datetime, timedelta, timezone

from .emailer import build_message, local_date, local_now, make_backend

BAR = "-" * 72


def _print_table(rows: list[tuple], headers: tuple[str, ...]) -> None:
    if not rows:
        print("  (none)")
        return
    widths = [len(h) for h in headers]
    cells = [[str(c) for c in row] for row in rows]
    for row in cells:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], min(len(cell), 42))
    print("  " + "  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("  " + "  ".join("-" * w for w in widths))
    for row in cells:
        print("  " + "  ".join(c[:42].ljust(w) for c, w in zip(row, widths)))


def build_digest(db, settings, days: int = 7, send_it: bool = True) -> str:
    """Weekly summary, mailed to the operator so nobody has to run stats."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    con = db.conn

    sent = con.execute(
        """SELECT s.email, s.sent_at, co.name AS company
           FROM sends s
           LEFT JOIN contacts c ON c.id = s.contact_id
           LEFT JOIN companies co ON co.id = c.company_id
           WHERE s.status = 'sent' AND s.sent_at >= ?
           ORDER BY s.sent_at""", (cutoff,)).fetchall()
    events = con.execute(
        "SELECT kind, ref, created_at FROM events WHERE created_at >= ? "
        "AND kind IN ('reply','opt_out','bounce') ORDER BY created_at",
        (cutoff,)).fetchall()
    stats = db.stats()
    queued = stats["queued"]
    pool = con.execute(
        "SELECT COUNT(*) FROM companies WHERE status = 'new'").fetchone()[0]
    replies = [e for e in events if e["kind"] == "reply"]
    opt_outs = [e for e in events if e["kind"] == "opt_out"]
    bounces = [e for e in events if e["kind"] == "bounce"]

    rate = (len(replies) / len(sent) * 100) if sent else 0.0
    bounce_rate = (len(bounces) / len(sent) * 100) if sent else 0.0

    lines = [
        f"Mailbot digest, last {days} days",
        "",
        f"  sent          {len(sent)}",
        f"  replies       {len(replies)}  ({rate:.0f}% of sent)",
        f"  bounces       {len(bounces)}  ({bounce_rate:.0f}% of sent)",
        f"  opt-outs      {len(opt_outs)}",
        "",
        f"  queued now    {queued}",
        f"  contacts      {stats['contacts']} total, "
        f"{stats['contacts_verified']} SMTP-verified",
        f"  companies     {stats['companies']} known, {pool} not yet enriched",
        f"  suppressed    {stats['suppressed']}",
    ]
    if replies:
        lines += ["", "Replies:"] + [f"  {e['ref']}" for e in replies]
    if bounces:
        lines += ["", "Bounces:"] + [f"  {e['ref']}" for e in bounces]
    if opt_outs:
        lines += ["", "Opted out:"] + [f"  {e['ref']}" for e in opt_outs]
    if sent:
        lines += ["", "Sent to:"] + [
            f"  {r['sent_at'][:10]}  {r['email']}  ({r['company'] or '?'})"
            for r in sent
        ]
    if bounce_rate > 5:
        lines += ["", "NOTE: bounce rate above 5%. Gmail penalises that. "
                      "Check verification before sending more."]
    if queued < settings.daily_send_limit:
        lines += ["", f"NOTE: only {queued} queued against a daily limit of "
                      f"{settings.daily_send_limit}. The nightly build may "
                      "need a higher limit."]
    body = "\n".join(lines) + "\n"

    if not send_it:
        return body
    subject = (f"Mailbot: {len(sent)} sent, {len(replies)} replies "
               f"(last {days}d)")
    message = build_message(
        subject, body, settings.from_email, settings.from_name,
        settings.from_email, settings.from_name,
    )
    backend = make_backend(settings)
    try:
        with backend:
            result = backend.send(message)
        print(f"digest: {'mailed' if result.ok else 'failed: ' + result.error}")
    except (smtplib.SMTPException, OSError) as exc:
        print(f"digest: could not send ({type(exc).__name__}); printing instead")
        print(body)
    return body


def print_summary(db, settings, fetcher, personalizer) -> None:
    """On-screen state: what went out today, and what budget is left."""
    today = local_date(settings.send_timezone)
    now = local_now(settings.send_timezone)
    rows = db.sends_on(today)
    print(f"\n{BAR}\nSENT TODAY  {today}  ({now:%H:%M} {settings.send_timezone})\n{BAR}")
    _print_table(
        [(r["sent_at"][11:16], r["email"], r["company_name"] or "",
          r["status"], (r["error"] or "")[:26]) for r in rows],
        ("time", "email", "company", "status", "error"),
    )
    stats = db.stats()
    used = db.sent_count_on(today)
    print(f"\n{BAR}\nQUOTAS AND PIPELINE\n{BAR}")
    _print_table(
        [
            ("daily sends", f"{used}/{settings.daily_send_limit}",
             f"{max(0, settings.daily_send_limit - used)} left"),
            ("http fetches", f"{fetcher.budget.used}/"
                             f"{fetcher.budget.limit}",
             f"{fetcher.budget.remaining} left"),
            ("groq calls", f"{personalizer.budget.used}/"
                           f"{personalizer.budget.limit}",
             "disabled" if not personalizer.enabled else
             f"{personalizer.budget.remaining} left"),
            ("companies", stats["companies"],
             f"{stats['companies_enriched']} enriched, "
             f"{stats['companies_no_contacts']} dry"),
            ("contacts", stats["contacts"],
             f"{stats['contacts_verified']} verified, "
             f"{stats['contacts_catchall']} catch-all"),
            ("queued", stats["queued"], ""),
            ("sent all time", stats["sent"], f"{stats['bounced']} bounced"),
            ("suppressed", stats["suppressed"], ""),
        ],
        ("metric", "value", "detail"),
    )