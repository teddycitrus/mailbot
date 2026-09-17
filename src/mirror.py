"""A copy of every queued draft in the Gmail Drafts folder.

The queue is a SQLite file on one laptop. Close the lid and nothing in it can
go out, however ready it is. A copy in Drafts is reachable from a phone, so a
shut laptop costs an hour rather than a day: the drafts are there, addressed,
personalised and carrying the resume, and sending one by hand takes a tap.

There are two ways a message could then go out twice, and both are closed:

  the bot sends it   the Gmail copy is deleted as soon as SMTP accepts it, so
                     nothing is left in Drafts to send by hand
  you send it        the Sent folder is read before every send run, and a
                     queued draft whose recipient already has mail from us is
                     recorded here as sent and dropped from the queue

Order matters in sync(). Reconcile and cleanup both remove reasons to send, so
they run before push, and a draft that has left by either route is never
mirrored again.

Nothing here sends mail. The only writes are an APPEND to Drafts and a DELETE
of something this module put there.
"""

from __future__ import annotations

import email as email_lib
import imaplib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime
from typing import Optional
from zoneinfo import ZoneInfo

from .emailer import draft_defect, local_date, message_for_draft
from .inbox import drafts_mailbox, save_draft, sent_mailbox


@dataclass
class MirrorReport:
    pushed: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    reconciled: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    def rows(self) -> list[tuple[str, str, str]]:
        return ([(e, "drafted", "copy in Gmail Drafts") for e in self.pushed]
                + [(e, "cleared", "sent, Gmail copy removed") for e in self.dropped]
                + [(e, "by hand", "found in Sent, marked sent here")
                   for e in self.reconciled]
                + [(e, "skipped", why) for e, why in self.skipped])


class GmailDrafts:
    """One IMAP connection to the Drafts and Sent folders.

    Both names are looked up by special-use flag, because Gmail calls them
    "[Gmail]/Drafts" and "[Gmail]/Sent Mail" and localises even that.
    """

    def __init__(self, settings):
        self.settings = settings
        self.conn: Optional[imaplib.IMAP4_SSL] = None
        self.drafts = "Drafts"
        self.sent = "Sent"

    def __enter__(self) -> "GmailDrafts":
        self.conn = imaplib.IMAP4_SSL(self.settings.imap_host,
                                      self.settings.imap_port)
        self.conn.login(self.settings.imap_user, self.settings.imap_pass)
        self.drafts = drafts_mailbox(self.conn)
        self.sent = sent_mailbox(self.conn)
        return self

    def __exit__(self, *_exc) -> None:
        try:
            self.conn.logout()
        except (imaplib.IMAP4.error, OSError):
            pass

    def push(self, message: EmailMessage) -> bool:
        return save_draft(self.conn, self.drafts, message)

    def drop(self, message_id: str) -> bool:
        """Remove a mirrored draft. Idempotent: one already gone counts as done.

        Deleted by UID rather than by flag-and-expunge, so a draft something
        else left flagged in this folder is not swept up with ours. Servers
        without UIDPLUS get the blunt form.
        """
        if not message_id:
            return False
        try:
            self.conn.select(self.drafts)
            typ, data = self.conn.uid("SEARCH", None, "HEADER", "Message-ID",
                                      '"' + message_id + '"')
            if typ != "OK":
                return False
            uids = (data[0] or b"").split()
            if not uids:
                return True
            for uid in uids:
                self.conn.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
            try:
                self.conn.uid("EXPUNGE", b",".join(uids).decode())
            except imaplib.IMAP4.error:
                self.conn.expunge()
        except (imaplib.IMAP4.error, OSError):
            return False
        return True

    def recipients_since(self, days: int, tz_name: str) -> dict[str, str]:
        """Addresses mailed from this account recently, and the day it happened.

        Headers only, fetched in one round trip. A full body fetch per message
        would cost as much as the whole inbox scan, and only the To and Date
        lines matter here. The date returned is the one the message carries
        rather than today's, so a message sent by hand on Tuesday counts
        against Tuesday's cap and not against the cap of the day it was seen.
        """
        found: dict[str, str] = {}
        since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
        try:
            self.conn.select(self.sent, readonly=True)
            typ, data = self.conn.search(None, "(SINCE " + since + ")")
            if typ != "OK" or not data or not data[0]:
                return found
            uids = data[0].split()
            typ, payload = self.conn.fetch(
                b",".join(uids).decode(), "(BODY.PEEK[HEADER.FIELDS (TO DATE)])")
            if typ != "OK":
                return found
        except (imaplib.IMAP4.error, OSError):
            return found
        for item in payload or []:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            header = email_lib.message_from_string(
                item[1].decode("utf-8", errors="replace"))
            day = _header_date(header.get("Date", ""), tz_name)
            for _name, addr in getaddresses(header.get_all("To", [])):
                if addr:
                    found[addr.lower()] = day
        return found


def _header_date(raw: str, tz_name: str) -> str:
    """The local date a Sent message carries, falling back to today."""
    try:
        return parsedate_to_datetime(raw).astimezone(
            ZoneInfo(tz_name)).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return local_date(tz_name)


def sync(settings, db, limit: int = 50, days: int = 7) -> MirrorReport:
    """Bring the Gmail Drafts folder and the local queue back into agreement."""
    settings.require_for_bounces()
    report = MirrorReport()
    with GmailDrafts(settings) as gmail:
        _reconcile(gmail, settings, db, days, report)
        _cleanup(gmail, db, report)
        _push(gmail, settings, db, limit, report)
    return report


def _reconcile(gmail: GmailDrafts, settings, db, days: int,
               report: MirrorReport) -> None:
    """Record the drafts that were sent by hand from Gmail.

    Matched on the recipient, not on the Message-ID: Gmail issues a fresh one
    when a draft is sent from its own client, so the id we appended does not
    survive the send. A queued draft whose recipient already has mail from
    this account has gone out, and the one safe reading is that it has gone
    out exactly once.

    This also catches a recipient mailed by hand for unrelated reasons, which
    is the behaviour we want anyway: nobody should get a cold email the day
    after a real one.
    """
    queued = db.queued_drafts(limit=500)
    if not queued:
        return
    already = gmail.recipients_since(days, settings.send_timezone)
    if not already:
        return
    for draft in queued:
        day = already.get(draft["email"].lower())
        if not day:
            continue
        db.record_send(draft["contact_id"], draft["email"], draft["subject"],
                       "", "gmail-manual", day)
        db.mark_draft(draft["id"], "sent")
        db.clear_draft_mirror(draft["id"])
        db.log("manual_send", draft["email"], "sent by hand on " + day)
        report.reconciled.append(draft["email"])


def _cleanup(gmail: GmailDrafts, db, report: MirrorReport) -> None:
    for row in db.stale_mirrors():
        if gmail.drop(row["gmail_message_id"]):
            db.clear_draft_mirror(row["id"])
            report.dropped.append(row["email"])


def _push(gmail: GmailDrafts, settings, db, limit: int,
          report: MirrorReport) -> None:
    for draft in db.drafts_to_mirror(limit):
        why = draft_defect(draft, settings)
        if why:
            # An incomplete draft must not reach the Drafts folder at all.
            # Sending one from a phone passes none of the checks send() makes.
            report.skipped.append((draft["email"], why))
            continue
        try:
            message, note = message_for_draft(settings, draft)
        except FileNotFoundError as exc:
            report.skipped.append((draft["email"], str(exc)))
            continue
        if not gmail.push(message):
            report.skipped.append((draft["email"], "IMAP refused the append"))
            continue
        db.set_draft_mirror(draft["id"], message["Message-ID"])
        report.pushed.append(draft["email"] + (" (resume link)" if note else ""))
