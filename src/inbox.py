"""Reading the mailbox: bounces, replies, and opt-out requests.

One IMAP pass classifies everything, rather than connecting once per concern.

The opt-out half exists because the template promises it. Every email says
"reply to this message and I will not contact you again", so a reply asking to
stop has to actually suppress the address. Honouring that is the difference
between outreach and spam.

The subtlety worth knowing: our own outgoing text contains the word
"unsubscribe", so a reply that quotes the original would match an opt-out
pattern on our own words. Quoted text is stripped before matching.

Every human reply also gets an answer drafted from the reply template and
saved to the Drafts folder over IMAP. Drafts only: nothing here sends mail.
"""

from __future__ import annotations

import email as email_lib
import imaplib
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

from .emailer import (
    Template, build_message, extract_bounced_addresses, is_hard_bounce,
    load_template, validate_template,
)
from .models import BOUNCED, REPLIED
from .verifier import first_name_from_email

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

AUTO_REPLY_SUBJECT = re.compile(
    r"(?i)^\s*(automatic reply|auto[- ]?reply|autoreply|auto:|out of (the )?office)"
)

RE_PREFIX = re.compile(r"(?i)^\s*(re\s*:\s*)+")

# One LIST response line: flags, hierarchy delimiter, mailbox name.
LIST_LINE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$')


@dataclass
class InboxScan:
    bounced: list[tuple[str, bool]] = field(default_factory=list)
    replied: list[str] = field(default_factory=list)
    opted_out: list[str] = field(default_factory=list)
    drafted: list[str] = field(default_factory=list)
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


# ------------------------------------------------------------ reply drafts

def header_text(value) -> str:
    """A header with any RFC 2047 encoded words decoded."""
    try:
        return str(make_header(decode_header(str(value or ""))))
    except Exception:
        return str(value or "")


def is_auto_reply(message) -> bool:
    """Out-of-office and other machine answers, which get no drafted reply."""
    submitted = str(message.get("Auto-Submitted", "")).strip().lower()
    if submitted and submitted != "no":
        return True
    if message.get("X-Autoreply") or message.get("X-Autorespond"):
        return True
    if str(message.get("Precedence", "")).strip().lower() in {"auto_reply", "bulk", "junk"}:
        return True
    return bool(AUTO_REPLY_SUBJECT.match(header_text(message.get("Subject", ""))))


def reply_first_name(contact_first: str, from_header: str) -> str:
    """Who to greet: the stored name, then the display name, then the address."""
    if contact_first:
        return contact_first
    display, address = parseaddr(from_header)
    words = header_text(display).replace('"', "").split()
    if words and words[0].isalpha() and len(words[0]) > 1:
        return words[0].capitalize()
    return first_name_from_email(address)


def load_reply_template(path: Path) -> Optional[Template]:
    """The reply template, or None to leave drafting off.

    A missing or broken template must not stop the scan, because opt-outs
    still have to be honoured, so drafting is simply skipped.
    """
    if not path.exists():
        return None
    try:
        template = load_template(path)
        validate_template(template)
    except ValueError as exc:
        print(f"inbox: reply drafts off, {exc}")
        return None
    return template


def build_reply_draft(template: Template, settings, incoming, first_name: str,
                      company: str) -> EmailMessage:
    """An answer to `incoming`, threaded under it. Saved as a draft, never sent.

    An unknown name or company is left as a visible [Name] or [company] hole,
    since a person reads every draft before it goes anywhere.
    """
    to_name, to_email = parseaddr(incoming.get("From", ""))
    subject, body = template.render({
        "first_name": first_name or "[Name]",
        # The name as stored. greeting_name would cut "Acme Data Co." down to
        # "Acme Data", which is not what the company is called.
        "company": company.strip() or "[company]",
        "original_subject": RE_PREFIX.sub("", header_text(incoming.get("Subject", ""))),
        "sender_name": settings.from_name,
        "sender_email": settings.from_email,
    })
    parent = str(incoming.get("Message-ID", "")).strip()
    draft = build_message(subject, body, to_email, header_text(to_name),
                          settings.from_email, settings.from_name,
                          settings.reply_to, in_reply_to=parent)
    if parent:
        chain = str(incoming.get("References", "")).split()
        draft.replace_header("References", " ".join(
            [ref for ref in chain if ref != parent] + [parent]))
    return draft


def drafts_mailbox(conn) -> str:
    """The Drafts folder, found by its special-use flag.

    Gmail names it "[Gmail]/Drafts" and localises even that, so the name is
    looked up rather than assumed.
    """
    try:
        typ, lines = conn.list()
    except imaplib.IMAP4.error:
        return "Drafts"
    for line in (lines or []) if typ == "OK" else []:
        found = LIST_LINE.match(line) if isinstance(line, bytes) else None
        if found and b"\\drafts" in found.group("flags").lower():
            return found.group("name").decode("utf-8", errors="replace").strip()
    return "Drafts"


def save_draft(conn, mailbox: str, draft: EmailMessage) -> bool:
    try:
        typ, _ = conn.append(mailbox, r"(\Draft \Seen)",
                             imaplib.Time2Internaldate(time.time()),
                             draft.as_bytes())
    except imaplib.IMAP4.error:
        return False
    return typ == "OK"


def scan_inbox(settings, db, days: int = 30, limit: int = 500) -> InboxScan:
    """Classify recent inbox mail against the people we have written to."""
    result = InboxScan()
    contacted = {
        row["email"].lower()
        for row in db.conn.execute(
            "SELECT DISTINCT email FROM sends WHERE status = 'sent'"
        )
    }
    # One draft per person. Later messages in the conversation are for a
    # human to answer, and redrafting on every scan would flood Drafts.
    drafted = {
        row["ref"]
        for row in db.conn.execute("SELECT ref FROM events WHERE kind = 'reply_draft'")
    }
    reply_template = load_reply_template(settings.reply_template_path)
    mailbox: Optional[str] = None
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
            row = db.find_contact_by_email(sender)
            if wants_out(body):
                db.suppress(sender, "opt-out reply")
                db.log("opt_out", sender, subject[:120])
                result.opted_out.append(sender)
            else:
                if row is not None and row["status"] != REPLIED:
                    db.set_contact_status(row["id"], REPLIED)
                    db.log("reply", sender, subject[:120])
                    result.replied.append(sender)

            if reply_template is None or sender in drafted or is_auto_reply(message):
                continue
            company = (db.get_company(row["company_id"])
                       if row is not None and row["company_id"] else None)
            draft = build_reply_draft(
                reply_template, settings, message,
                reply_first_name(row["first_name"] if row is not None else "",
                                 message.get("From", "")),
                company["name"] if company is not None else "",
            )
            if mailbox is None:
                mailbox = drafts_mailbox(conn)
            if save_draft(conn, mailbox, draft):
                drafted.add(sender)
                db.log("reply_draft", sender, subject[:120])
                result.drafted.append(sender)
            else:
                print(f"inbox: could not save a reply draft for {sender} to {mailbox}")
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return result
