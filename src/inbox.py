"""Reading the mailbox: bounces, replies, and opt-out requests.

One IMAP pass classifies everything, rather than connecting once per concern.

The opt-out half exists because the template promises it. Every email says
"reply to this message and I will not contact you again", so a reply asking to
stop has to actually suppress the address. Honouring that is the difference
between outreach and spam.

The subtlety worth knowing: our own outgoing text contains the word
"unsubscribe", so a reply that quotes the original would match an opt-out
pattern on our own words. Quoted text is stripped before matching.
"""

from __future__ import annotations

import email as email_lib
import imaplib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.utils import parseaddr

from .emailer import extract_bounced_addresses, is_hard_bounce
from .models import BOUNCED, REPLIED

BOUNCE_SENDERS = ("mailer-daemon", "postmaster", "mail delivery", "mailer_daemon")

OPT_OUT = re.compile(
    r"(?i)\b(unsubscribe|remove me|take me off|stop emailing|stop contacting|"
    r"do not (?:contact|email) me|don't (?:contact|email) me|not interested|"
    r"no longer interested|please stop)\b"
)

# Where a quoted original typically begins.
QUOTE_MARKERS = (
    re.compile(r"(?im)^\s*on .{0,80}wrote:\s*$"),
    re.compile(r"(?im)^\s*-+\s*original message\s*-+\s*$"),
    re.compile(r"(?im)^\s*from:\s.+$"),
    re.compile(r"(?im)^\s*_{5,}\s*$"),
)


@dataclass
class InboxScan:
    bounced: list[tuple[str, bool]] = field(default_factory=list)
    replied: list[str] = field(default_factory=list)
    opted_out: list[str] = field(default_factory=list)
    examined: int = 0


def strip_quoted(text: str) -> str:
    """Keep only what the person actually typed, not the mail they quoted."""
    cut = len(text)
    for marker in QUOTE_MARKERS:
        found = marker.search(text)
        if found and found.start() < cut:
            cut = found.start()
    head = text[:cut]
    return "\n".join(l for l in head.splitlines() if not l.lstrip().startswith(">"))


def wants_out(body: str) -> bool:
    return bool(OPT_OUT.search(strip_quoted(body)))


def plain_body(message) -> str:
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain":
                try:
                    return part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
                except Exception:
                    continue
        return ""
    try:
        return message.get_payload(decode=True).decode(
            message.get_content_charset() or "utf-8", errors="replace"
        )
    except Exception:
        return ""


def is_bounce(from_addr: str, subject: str) -> bool:
    blob = f"{from_addr} {subject}".lower()
    return any(s in blob for s in BOUNCE_SENDERS) or "undeliverable" in blob


def scan_inbox(settings, db, days: int = 30, limit: int = 500) -> InboxScan:
    """Classify recent inbox mail against the people we have written to."""
    result = InboxScan()
    contacted = {
        row["email"].lower()
        for row in db.conn.execute(
            "SELECT DISTINCT email FROM sends WHERE status = 'sent'"
        )
    }
    own_domain = settings.from_email.split("@")[-1] if settings.from_email else ""

    conn = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
    try:
        conn.login(settings.imap_user, settings.imap_pass)
        conn.select("INBOX")
        since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
        typ, data = conn.search(None, f'(SINCE {since})')
        if typ != "OK" or not data or not data[0]:
            return result
        uids = data[0].split()[-limit:]
        for uid in uids:
            typ, payload = conn.fetch(uid, "(RFC822)")
            if typ != "OK" or not payload or not isinstance(payload[0], tuple):
                continue
            raw = payload[0][1].decode("utf-8", errors="replace")
            result.examined += 1
            message = email_lib.message_from_string(raw)
            sender = parseaddr(message.get("From", ""))[1].lower()
            subject = str(message.get("Subject", ""))

            if is_bounce(sender, subject):
                hard = is_hard_bounce(raw)
                for addr in extract_bounced_addresses(raw, own_domain):
                    if db.find_contact_by_email(addr) is None:
                        continue
                    db.suppress(addr, "hard bounce" if hard else "soft bounce")
                    db.conn.execute(
                        "UPDATE contacts SET status = ? WHERE email = ?",
                        (BOUNCED, addr),
                    )
                    db.conn.commit()
                    db.log("bounce", addr, "hard" if hard else "soft")
                    result.bounced.append((addr, hard))
                continue

            if sender not in contacted:
                continue

            body = plain_body(message)
            if wants_out(body):
                db.suppress(sender, "opt-out reply")
                db.log("opt_out", sender, subject[:120])
                result.opted_out.append(sender)
            else:
                row = db.find_contact_by_email(sender)
                if row is not None and row["status"] != REPLIED:
                    db.set_contact_status(row["id"], REPLIED)
                    db.log("reply", sender, subject[:120])
                    result.replied.append(sender)
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return result
