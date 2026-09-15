"""Template rendering, message construction, delivery and bounce handling.

Two backends, one message builder. Every outgoing message carries List-
Unsubscribe headers and a plain-text body, because a job-search email that
looks like marketing gets filtered like marketing.

The send window matters as much as the content: sending is refused outside the
configured local hours so a scheduled run that fires late does not deliver at
3am.
"""

from __future__ import annotations

import json
import mimetypes
import random
import re
import smtplib
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, time as dtime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid, parseaddr
from pathlib import Path
from string import Formatter
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from .models import SendResult

RESEND_URL = "https://api.resend.com/emails"


# ---------------------------------------------------------------- templates

class _Safe(dict):
    """Missing placeholders render empty instead of raising mid-run."""

    def __missing__(self, key: str) -> str:
        return ""


@dataclass
class Template:
    subject: str
    body: str

    @property
    def placeholders(self) -> set[str]:
        fields = set()
        for text in (self.subject, self.body):
            for _, field, _, _ in Formatter().parse(text):
                if field:
                    fields.add(field)
        return fields

    def render(self, context: dict[str, str]) -> tuple[str, str]:
        safe = _Safe(context)
        subject = Formatter().vformat(self.subject, (), safe).strip()
        body = Formatter().vformat(self.body, (), safe)
        body = re.sub(r"\n{3,}", "\n\n", body).strip() + "\n"
        return subject, body


# Everything the pipeline knows how to fill in. Anything else in a template is
# a hole the author forgot to close, and would otherwise ship as blank space.
KNOWN_FIELDS = frozenset({
    "first_name", "full_name", "company", "role", "location", "batch",
    "one_liner", "personal_note", "sender_name", "sender_email", "unsubscribe",
    "original_subject", "resume_link",
})


# The resume travels as an attachment. The hosted link is the fallback for the
# one case the attachment cannot cover, and the two never ship together: Gmail
# renders a Drive link as its own chip beside the attachment, so a message
# carrying both looks like it holds two different resumes.
RESUME_LINK_LABEL = "View my resume"


def resume_link_line(url: str) -> str:
    """The signature line offering a hosted resume, or nothing if unconfigured."""
    return f"[{RESUME_LINK_LABEL}]({url})" if url else ""


def has_resume_link(body: str, url: str = "") -> bool:
    return bool(url and url in body) or f"[{RESUME_LINK_LABEL}](" in body


def with_resume_link(body: str, url: str) -> str:
    """Append the hosted link to a body whose attachment did not make it.

    Idempotent, so a body rendered from a template that already carries the
    link is left alone rather than growing a second copy.
    """
    if not url or has_resume_link(body, url):
        return body
    return body.rstrip("\n") + f"\n{resume_link_line(url)}\n"


UNEDITED_MARKER = re.compile(r"\[[A-Z][A-Z0-9 ,.'-]{3,}\]")


def unedited_markers(text: str) -> list[str]:
    """Bracketed ALL-CAPS holes the author has not filled in yet.

    Queuing these is fine and useful: the draft still shows the real company,
    the real recipient and the real opening line, so it can be previewed. What
    must never happen is one of them going out, which send() enforces.
    """
    return sorted(set(UNEDITED_MARKER.findall(text)))


def validate_template(template: Template,
                      known: frozenset[str] = KNOWN_FIELDS) -> None:
    """Reject placeholders the pipeline cannot fill. Those are author errors."""
    unknown = sorted(template.placeholders - set(known))
    if unknown:
        raise ValueError(
            "template has placeholders the pipeline cannot fill: "
            + ", ".join("{" + u + "}" for u in unknown)
            + ". Replace them with your own text, or use one of: "
            + ", ".join("{" + k + "}" for k in sorted(known))
        )


def load_template(path: str | Path) -> Template:
    """Template file is 'Subject: ...' on line one, blank line, then the body."""
    return parse_template(Path(path).read_text(encoding="utf-8"), str(path))


def parse_template(text: str, path: str = "template") -> Template:
    """The same rules as load_template, for text that is not in a file yet."""
    lines = text.splitlines()
    if not lines:
        raise ValueError(f"template {path} is empty")
    if not lines[0].lower().startswith("subject:"):
        raise ValueError(
            f"template {path} must start with a 'Subject:' line, got {lines[0][:40]!r}"
        )
    subject = lines[0].split(":", 1)[1].strip()
    body = "\n".join(lines[1:]).lstrip("\n")
    if not body.strip():
        raise ValueError(f"template {path} has no body")
    return Template(subject=subject, body=body)


# ------------------------------------------------------------- send window

def local_now(tz_name: str) -> datetime:
    return datetime.now(ZoneInfo(tz_name))


def local_date(tz_name: str) -> str:
    return local_now(tz_name).date().isoformat()


def in_send_window(tz_name: str, start: dtime, end: dtime,
                   now: Optional[datetime] = None) -> tuple[bool, str]:
    current = (now or local_now(tz_name))
    clock = current.time()
    if current.weekday() >= 5:
        return False, f"weekend ({current:%A})"
    if start <= clock <= end:
        return True, ""
    return False, (
        f"{clock:%H:%M} {tz_name} outside window "
        f"{start:%H:%M}-{end:%H:%M}"
    )


# Where the target companies actually are. Sending at 08:30 Toronto puts an
# email in a San Francisco inbox at 05:30 local, where it is buried by the time
# anyone reads mail. Matching the window to the recipient's own morning is the
# point of this table.
LOCATION_TZ: tuple[tuple[str, str], ...] = (
    ("san francisco", "America/Los_Angeles"),
    ("bay area", "America/Los_Angeles"),
    ("palo alto", "America/Los_Angeles"),
    ("mountain view", "America/Los_Angeles"),
    ("menlo park", "America/Los_Angeles"),
    ("oakland", "America/Los_Angeles"),
    ("berkeley", "America/Los_Angeles"),
    ("los angeles", "America/Los_Angeles"),
    ("san jose", "America/Los_Angeles"),
    ("seattle", "America/Los_Angeles"),
    ("portland", "America/Los_Angeles"),
    ("vancouver", "America/Vancouver"),
    ("denver", "America/Denver"),
    ("austin", "America/Chicago"),
    ("chicago", "America/Chicago"),
    ("new york", "America/New_York"),
    ("brooklyn", "America/New_York"),
    ("manhattan", "America/New_York"),
    ("boston", "America/New_York"),
    ("toronto", "America/Toronto"),
    ("waterloo", "America/Toronto"),
    ("ottawa", "America/Toronto"),
    ("montreal", "America/Toronto"),
    ("london", "Europe/London"),
    ("berlin", "Europe/Berlin"),
    ("paris", "Europe/Paris"),
    ("bangalore", "Asia/Kolkata"),
    ("bengaluru", "Asia/Kolkata"),
    ("singapore", "Asia/Singapore"),
)


def recipient_timezone(location: str, default: str) -> str:
    """Best-effort timezone for a company location string.

    Falls back to the sender's own zone, which keeps behaviour unchanged for
    anywhere the table does not recognise.
    """
    low = (location or "").lower()
    for needle, zone in LOCATION_TZ:
        if needle in low:
            return zone
    return default


# Legal suffixes read badly in a salutation: "Dear Datrics Inc.," is worse
# than "Dear Datrics,". Brand words like AI or Labs are left alone, because
# they are usually part of how the company actually calls itself.
LEGAL_SUFFIX = re.compile(
    r"[,\s]+(inc|llc|ltd|limited|corp|corporation|co|gmbh|bv|sa|plc)\.?$",
    re.IGNORECASE,
)


def greeting_name(company: str) -> str:
    """The company as you would address it in a salutation."""
    return LEGAL_SUFFIX.sub("", (company or "").strip()).strip()


def local_window_open(location: str, default_tz: str, start: dtime, end: dtime,
                      now_utc: Optional[datetime] = None) -> tuple[bool, str]:
    """Is it currently inside the send window where the recipient sits?"""
    zone = recipient_timezone(location, default_tz)
    moment = (now_utc or datetime.now(timezone.utc)).astimezone(ZoneInfo(zone))
    if moment.weekday() >= 5:
        return False, f"weekend in {zone}"
    if start <= moment.time() <= end:
        return True, zone
    return False, f"{moment:%H:%M} {zone}"


def pace_delay(base_seconds: int) -> float:
    """Jitter the gap so a run does not look metronomic."""
    return max(1.0, base_seconds * random.uniform(0.6, 1.6))


# ------------------------------------------------------ markdown rendering

MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def md_to_plain(text: str) -> str:
    """Flatten links for the text/plain part: [Eureka](url) -> Eureka (url).

    A bare link whose label already is the URL collapses to just the URL.
    """
    def repl(m: re.Match) -> str:
        label, url = m.group(1), m.group(2)
        return url if label.strip() == url.strip() else f"{label} ({url})"

    return MD_LINK.sub(repl, text)


def _esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def md_to_html(text: str) -> str:
    """Render the template as simple HTML, keeping links clickable."""
    out: list[str] = []
    in_list = False
    for raw in text.split("\n"):
        line = raw.rstrip()
        bullet = line.lstrip().startswith("- ")
        if bullet and not in_list:
            out.append("<ul>")
            in_list = True
        elif not bullet and in_list:
            out.append("</ul>")
            in_list = False
        content = line.lstrip()[2:] if bullet else line
        # Escape first, then substitute links so hrefs are not mangled.
        html = MD_LINK.sub(
            lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>',
            _esc(content),
        )
        if bullet:
            out.append(f"  <li>{html}</li>")
        elif not html.strip():
            out.append("<br>")
        else:
            out.append(f"<div>{html}</div>")
    if in_list:
        out.append("</ul>")
    body = "\n".join(out)
    return (
        '<html><body style="font-family:-apple-system,Segoe UI,Arial,sans-serif;'
        'font-size:14px;line-height:1.5;color:#222">\n' + body + "\n</body></html>"
    )


# ---------------------------------------------------------- message build

def build_message(subject: str, body: str, to_email: str, to_name: str,
                  from_email: str, from_name: str, reply_to: str = "",
                  unsubscribe_mailto: str = "",
                  attachments: Iterable[str | Path] = (),
                  in_reply_to: str = "") -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{from_email}>" if from_name else from_email
    msg["To"] = f"{to_name} <{to_email}>" if to_name else to_email
    msg["Date"] = format_datetime(datetime.now().astimezone())
    msg["Message-ID"] = make_msgid(domain=from_email.split("@")[-1] or None)
    if reply_to:
        msg["Reply-To"] = reply_to
    if in_reply_to:
        # Threads the follow-up under the original so it reads as a bump
        # rather than a second cold email.
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    if unsubscribe_mailto:
        msg["List-Unsubscribe"] = f"<mailto:{unsubscribe_mailto}?subject=unsubscribe>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg["Auto-Submitted"] = "no"
    # Plain text is the body every client can read; the HTML alternative keeps
    # the links in the signature clickable.
    msg.set_content(md_to_plain(body))
    msg.add_alternative(md_to_html(body), subtype="html")

    for item in attachments:
        path = Path(item)
        if not path.exists():
            raise FileNotFoundError(f"attachment not found: {path}")
        ctype, _ = mimetypes.guess_type(path.name)
        maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
        msg.add_attachment(
            path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name
        )
    return msg


# ------------------------------------------------------------- backends

class SMTPBackend:
    name = "smtp"

    def __init__(self, host: str, port: int, user: str, password: str):
        self.host, self.port, self.user, self.password = host, port, user, password
        self._conn: Optional[smtplib.SMTP] = None

    def __enter__(self) -> "SMTPBackend":
        context = ssl.create_default_context()
        if self.port == 465:
            self._conn = smtplib.SMTP_SSL(self.host, self.port, timeout=30,
                                          context=context)
        else:
            self._conn = smtplib.SMTP(self.host, self.port, timeout=30)
            self._conn.ehlo()
            self._conn.starttls(context=context)
            self._conn.ehlo()
        self._conn.login(self.user, self.password)
        return self

    def __exit__(self, *_exc) -> None:
        if self._conn is not None:
            try:
                self._conn.quit()
            except Exception:
                pass
            self._conn = None

    def send(self, msg: EmailMessage) -> SendResult:
        if self._conn is None:
            return SendResult(False, error="SMTP connection not open", backend=self.name)
        try:
            self._conn.send_message(msg)
            return SendResult(True, message_id=msg["Message-ID"], backend=self.name)
        except smtplib.SMTPRecipientsRefused as exc:
            return SendResult(False, error=f"recipient refused: {exc.recipients}",
                              backend=self.name)
        except Exception as exc:
            return SendResult(False, error=f"{type(exc).__name__}: {exc}",
                              backend=self.name)


class ResendBackend:
    name = "resend"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def __enter__(self) -> "ResendBackend":
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def send(self, msg: EmailMessage) -> SendResult:
        body = msg.get_body(preferencelist=("plain",))
        payload: dict = {
            "from": msg["From"],
            "to": [parseaddr(msg["To"])[1]],
            "subject": msg["Subject"],
            "text": body.get_content() if body else "",
        }
        if msg["Reply-To"]:
            payload["reply_to"] = msg["Reply-To"]
        attachments = []
        for part in msg.iter_attachments():
            import base64
            attachments.append({
                "filename": part.get_filename() or "attachment",
                "content": base64.b64encode(part.get_payload(decode=True)).decode(),
            })
        if attachments:
            payload["attachments"] = attachments

        request = urllib.request.Request(
            RESEND_URL, data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as resp:
                data = json.loads(resp.read().decode())
            return SendResult(True, message_id=data.get("id", ""), backend=self.name)
        except urllib.error.HTTPError as exc:
            return SendResult(False, error=f"HTTP {exc.code}: {exc.read()[:200]!r}",
                              backend=self.name)
        except Exception as exc:
            return SendResult(False, error=f"{type(exc).__name__}: {exc}",
                              backend=self.name)


def make_backend(settings):
    if settings.email_backend == "resend":
        return ResendBackend(settings.resend_api_key)
    return SMTPBackend(settings.smtp_host, settings.smtp_port,
                       settings.smtp_user, settings.smtp_pass)


# --------------------------------------------------------------- bounces

BOUNCE_SENDERS = ("mailer-daemon", "postmaster", "mail delivery subsystem")
HARD_BOUNCE_CODES = ("5.1.1", "5.1.2", "5.1.10", "5.2.1", "5.4.1", "550", "553")
ADDR_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
FAILURE_WORDS = re.compile(
    r"(?i)\b(failed|failure|undelivered|undeliverable|unknown|not found|"
    r"rejected|does not exist|no such user|blocked|refused)\b"
)


def extract_bounced_addresses(raw_message: str, own_domain: str = "") -> set[str]:
    """Pull the failed recipient out of a delivery status notification."""
    found: set[str] = set()
    for match in re.finditer(r"(?i)final-recipient:\s*rfc822;\s*([^\s]+)", raw_message):
        found.add(match.group(1).strip().lower().strip("<>"))
    if not found:
        # No DSN headers, so fall back to prose. The failure wording can sit on
        # either side of the address ("could not deliver to x@y" as well as
        # "x@y: user unknown"), so look in a window around each address.
        for match in ADDR_RE.finditer(raw_message):
            start = max(0, match.start() - 90)
            window = raw_message[start:match.end() + 90].lower()
            if FAILURE_WORDS.search(window):
                found.add(match.group(0).lower())
    if own_domain:
        found = {a for a in found if not a.endswith("@" + own_domain.lower())}
    return {a for a in found if ADDR_RE.match(a)}


def is_hard_bounce(raw_message: str) -> bool:
    lowered = raw_message.lower()
    return any(code in lowered for code in HARD_BOUNCE_CODES)


def scan_bounces(settings, db, limit: int = 200) -> list[tuple[str, bool]]:
    """Read the mailbox for delivery failures and suppress the addresses."""
    import imaplib
    import email as email_lib

    results: list[tuple[str, bool]] = []
    own_domain = settings.from_email.split("@")[-1] if settings.from_email else ""
    conn = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
    try:
        conn.login(settings.imap_user, settings.imap_pass)
        conn.select("INBOX")
        seen: set[str] = set()
        for term in ('(FROM "mailer-daemon")', '(FROM "postmaster")',
                     '(SUBJECT "Undelivered")', '(SUBJECT "Delivery Status")'):
            typ, data = conn.search(None, term)
            if typ != "OK" or not data or not data[0]:
                continue
            for uid in data[0].split()[-limit:]:
                if uid in seen:
                    continue
                seen.add(uid)
                typ, payload = conn.fetch(uid, "(RFC822)")
                if typ != "OK" or not payload or not isinstance(payload[0], tuple):
                    continue
                raw = payload[0][1].decode("utf-8", errors="replace")
                hard = is_hard_bounce(raw)
                for addr in extract_bounced_addresses(raw, own_domain):
                    if db.find_contact_by_email(addr) is None:
                        continue
                    db.suppress(addr, "hard bounce" if hard else "soft bounce")
                    db.log("bounce", addr, "hard" if hard else "soft")
                    results.append((addr, hard))
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return results
