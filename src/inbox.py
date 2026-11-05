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

Gmail sometimes files a real reply as spam, where nobody reads it and the
follow-up goes out anyway. So once a day the Spam folder gets the same pass,
and anything from someone we wrote to is moved back to the inbox.
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
    Template, build_message, extract_bounced_addresses, is_delay_notice, is_hard_bounce,
    load_template, strip_re_prefix, validate_template,
)
from .models import BOUNCED, REPLIED
from .hn_source import CONSUMER
from .verifier import first_name_from_email

BOUNCE_SENDERS = ("mailer-daemon", "postmaster", "mail delivery", "mailer_daemon")

# Built from a corpus of synthetic refusals (tests/test_optout_corpus.py).
# The first version knew ten phrasings and missed half the corpus: opt out,
# stop sending, remove us, reach out, leave me alone. Verbs stay bound to
# their objects so "We stopped using Django" and "I removed the posting"
# never read as refusals.
OPT_OUT = re.compile(
    r"(?i)\b(unsubscribe|unsub|opt (?:me |us )?out|"
    r"remove (?:me|us|this address)|take (?:me|us) off|"
    r"stop (?:emailing|contacting|sending|messaging)|"
    r"(?:do not|don't) (?:contact|email|message|send|write to) (?:me|us)|"
    r"(?:do not|don't) reach out|(?:do not|don't) send (?:any ?)?more|"
    r"not interested|no longer interested|please stop|leave me alone|"
    r"cease (?:all )?(?:communication|contact)|no more (?:e-?mails?|messages)|"
    # French, Spanish, German. Same rule: the verb stays bound to its object.
    r"ne plus (?:me|nous) (?:contacter|[ée]crire)|me retirer de (?:votre|vos|la|cette) liste|retirez[- ]moi|"
    r"d[ée]sabonn\w*|pas int[ée]ress[ée]e?s?|"
    r"no (?:me|nos) contact(?:e|en|es)|dar(?:me|nos)? de baja|"
    r"no (?:estoy|estamos) interesad[oa]s?|"
    r"abmelden|keine (?:e-?mails?|nachrichten) mehr|kein interesse)\b"
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

# One LIST response line: flags, hierarchy delimiter, mailbox name.
LIST_LINE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$')


@dataclass
class InboxScan:
    bounced: list[tuple[str, bool]] = field(default_factory=list)
    replied: list[str] = field(default_factory=list)
    opted_out: list[str] = field(default_factory=list)
    drafted: list[str] = field(default_factory=list)
    # Replies found in Spam and moved back to the inbox.
    rescued: list[str] = field(default_factory=list)
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


def is_refusal(message) -> bool:
    """The body says stop, or the subject does.

    The List-Unsubscribe mailto on every message makes the mail client send
    one with the subject "unsubscribe" and a body of its own boilerplate.
    """
    return (wants_out(plain_body(message))
            or bool(OPT_OUT.search(header_text(message.get("Subject", "")))))


def _colleague_refusal(db, sender: str, message):
    """The contact a fresh refusal from their company's domain speaks for."""
    domain = sender.rpartition("@")[2]
    if (not domain or domain in CONSUMER or message.get("List-Unsubscribe")
            or message.get("List-Id") or is_auto_reply(message)):
        return None
    if not is_refusal(message):
        return None
    return db.last_contacted_at_domain(domain)


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
        "original_subject": strip_re_prefix(header_text(incoming.get("Subject", ""))),
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


def special_mailbox(conn, flag: bytes, fallback: str) -> str:
    """A folder found by its special-use flag rather than by name.

    Gmail names these "[Gmail]/Drafts" and "[Gmail]/Sent Mail", and localises
    even that, so the name is looked up rather than assumed.
    """
    try:
        typ, lines = conn.list()
    except imaplib.IMAP4.error:
        return fallback
    for line in (lines or []) if typ == "OK" else []:
        found = LIST_LINE.match(line) if isinstance(line, bytes) else None
        if found and flag in found.group("flags").lower():
            return found.group("name").decode("utf-8", errors="replace").strip()
    return fallback


def drafts_mailbox(conn) -> str:
    return special_mailbox(conn, rb"\drafts", "Drafts")


def sent_mailbox(conn) -> str:
    return special_mailbox(conn, rb"\sent", "Sent")


def spam_mailbox(conn) -> str:
    return special_mailbox(conn, rb"\junk", "[Gmail]/Spam")


def save_draft(conn, mailbox: str, draft: EmailMessage) -> bool:
    try:
        typ, _ = conn.append(mailbox, r"(\Draft \Seen)",
                             imaplib.Time2Internaldate(time.time()),
                             draft.as_bytes())
    except imaplib.IMAP4.error:
        return False
    return typ == "OK"


def scan_inbox(settings, db, days: int = 30, limit: int = 500,
               spam: bool = False) -> InboxScan:
    """Classify recent inbox mail against the people we have written to.

    With `spam`, the Spam folder instead. Replies and opt-outs found there
    count exactly as in the inbox, and are moved to the inbox so they get
    read; the rest of Spam is left alone.
    """
    result = InboxScan()
    contacted = {
        row["email"].lower()
        for row in db.conn.execute(
            "SELECT DISTINCT email FROM sends WHERE status IN ('sent', 'sending')"
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
        typ, _ = conn.select(spam_mailbox(conn) if spam else "INBOX")
        if typ != "OK":
            return result
        since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
        typ, data = conn.uid("SEARCH", None, f'(SINCE {since})')
        if typ != "OK" or not data or not data[0]:
            return result
        uids = data[0].split()[-limit:]
        rescue: list[bytes] = []
        for uid in uids:
            typ, payload = conn.uid("FETCH", uid, "(BODY.PEEK[])")
            if typ != "OK" or not payload or not isinstance(payload[0], tuple):
                continue
            raw = payload[0][1].decode("utf-8", errors="replace")
            result.examined += 1
            message = email_lib.message_from_string(raw)
            sender = parseaddr(message.get("From", ""))[1].lower()
            subject = str(message.get("Subject", ""))

            if is_bounce(sender, subject):
                if is_delay_notice(raw):
                    continue
                hard = is_hard_bounce(raw)
                for addr in extract_bounced_addresses(raw, own_domain):
                    # Each scan rereads every notice still in the inbox, so
                    # one already recorded must not be logged again.
                    row = db.find_contact_by_email(addr)
                    if row is None or row["status"] == BOUNCED:
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

            if sender in contacted:
                row = db.find_contact_by_email(sender)
                who = sender
            else:
                # Someone answering in a thread we started from an address we
                # never wrote to: a CEO replying for the git@ or applications@
                # mailbox. Missing these left the company open to a bump after
                # it had already said no.
                if sender == settings.from_email.lower():
                    continue
                row = db.contact_for_thread(
                    header_text(message.get("In-Reply-To", "")).split()
                    + header_text(message.get("References", "")).split())
                if row is None:
                    # A colleague writing fresh from the company's own domain
                    # to say stop. Only a refusal counts here, never mailing
                    # lists, and never a free-mail host anyone can write from.
                    row = _colleague_refusal(db, sender, message)
                    if row is None:
                        continue
                    if not db.is_suppressed(sender):
                        db.suppress(sender, "opt-out reply")
                    if not db.is_suppressed(row["email"]):
                        db.suppress(row["email"], "opt-out reply")
                        db.log("opt_out", row["email"], f"via {sender}: {subject[:100]}")
                        result.opted_out.append(row["email"])
                    continue
                who = row["email"]

            if spam and not is_auto_reply(message):
                rescue.append(uid)
                result.rescued.append(sender)

            if is_refusal(message):
                if not db.is_suppressed(who):
                    db.suppress(who, "opt-out reply")
                    db.log("opt_out", who, subject[:120])
                    result.opted_out.append(who)
            else:
                if row is not None and row["status"] != REPLIED:
                    db.set_contact_status(row["id"], REPLIED)
                    db.log("reply", who, subject[:120])
                    result.replied.append(who)

            if reply_template is None or who in drafted or is_auto_reply(message):
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
        for uid in rescue:
            typ, _ = conn.uid("MOVE", uid, "INBOX")
            if typ == "OK":
                db.log("spam_rescue", "", uid.decode())
            else:
                print(f"inbox: could not move spam message {uid.decode()} to the inbox")
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return result
