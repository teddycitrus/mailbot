"""The Gmail Drafts mirror.

The mirror exists so a closed laptop does not cost a day, and everything that
matters here is about the cost of that convenience: a copy sitting in Drafts
is a message a human can send, so it must never be an unfinished one, and it
must not still be there once the bot has sent it. The reconcile direction is
the other half, catching a draft that went out by hand before the scheduled
run sends it again.
"""

from __future__ import annotations

import imaplib
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import mirror


class FakeIMAP:
    """Enough of imaplib to exercise push, drop and the Sent scan."""

    def __init__(self, sent=(), drafts=()):
        self.appended = []
        self.stored = []
        self.expunged = []
        self.selected = None
        self._sent = list(sent)
        self._drafts = list(drafts)
        self.uidplus = True

    def select(self, mailbox, readonly=False):
        self.selected = mailbox
        return "OK", [b"1"]

    def append(self, mailbox, flags, date, message):
        self.appended.append((mailbox, flags, message))
        return "OK", [b"[APPENDUID 1 2] (Success)"]

    def search(self, _charset, query):
        return "OK", [b" ".join(str(i + 1).encode()
                                for i in range(len(self._sent)))]

    def fetch(self, _uids, _parts):
        return "OK", [(b"1 (BODY[HEADER.FIELDS (TO DATE)]", raw)
                      for raw in self._sent]

    def uid(self, command, *args):
        if command == "SEARCH":
            wanted = args[-1].strip('"')
            hits = [str(i + 1).encode() for i, m in enumerate(self._drafts)
                    if m == wanted]
            return "OK", [b" ".join(hits)]
        if command == "STORE":
            self.stored.append(args[0])
            return "OK", [b""]
        if command == "EXPUNGE":
            if not self.uidplus:
                raise imaplib.IMAP4.error("UID EXPUNGE not supported")
            self.expunged.append(args[0])
            return "OK", [b""]
        raise AssertionError(f"unexpected uid command {command}")

    def expunge(self):
        self.expunged.append("all")
        return "OK", [b""]

    def logout(self):
        return "BYE", [b""]


def gmail(conn):
    obj = object.__new__(mirror.GmailDrafts)
    obj.settings = SimpleNamespace()
    obj.conn = conn
    obj.drafts = "[Gmail]/Drafts"
    obj.sent = "[Gmail]/Sent Mail"
    return obj


def settings(tmp_path, **over):
    pdf = tmp_path / "resume.pdf"
    if not pdf.exists():
        pdf.write_bytes(b"%PDF-1.4")
    base = dict(from_email="me@example.com", from_name="Me",
                reply_to="me@example.com", unsubscribe_mailto="me@example.com",
                resume_path=pdf, resume_link="", send_timezone="America/Toronto")
    base.update(over)
    return SimpleNamespace(**base)


def draft(tmp_path, **over):
    base = dict(id=1, contact_id=7, subject="Hi Acme", body="Hello Stefan\n",
                email="a@acme.ai", first_name="Stefan", last_name="",
                attachments=str(tmp_path / "resume.pdf"),
                gmail_message_id="", company_name="Acme")
    base.update(over)
    return base


# ------------------------------------------------------------------ drop

def test_drop_deletes_only_the_matching_uid():
    conn = FakeIMAP(drafts=["<a@x>", "<b@x>"])
    assert gmail(conn).drop("<b@x>")
    assert conn.stored == [b"2"]
    assert conn.expunged == ["2"]


def test_drop_of_a_copy_already_gone_is_a_success():
    # The user deleted it themselves. Nothing left to do, and reporting this
    # as a failure would make the caller retry it forever.
    conn = FakeIMAP(drafts=[])
    assert gmail(conn).drop("<gone@x>")
    assert conn.stored == []


def test_drop_falls_back_to_plain_expunge_without_uidplus():
    conn = FakeIMAP(drafts=["<a@x>"])
    conn.uidplus = False
    assert gmail(conn).drop("<a@x>")
    assert conn.expunged == ["all"]


def test_drop_without_a_message_id_does_nothing():
    conn = FakeIMAP(drafts=["<a@x>"])
    assert not gmail(conn).drop("")
    assert conn.stored == []


def test_drop_survives_an_imap_error():
    class Broken(FakeIMAP):
        def select(self, mailbox, readonly=False):
            raise imaplib.IMAP4.error("connection reset")

    assert not gmail(Broken()).drop("<a@x>")


# -------------------------------------------------------------- Sent scan

def test_recipients_since_reads_addresses_and_their_date():
    conn = FakeIMAP(sent=[b"To: a@acme.ai\r\nDate: Tue, 15 Sep 2026 09:02:00 -0400\r\n"])
    found = gmail(conn).recipients_since(7, "America/Toronto")
    assert found == {"a@acme.ai": "2026-09-15"}


def test_recipients_since_handles_several_recipients():
    conn = FakeIMAP(sent=[b"To: a@acme.ai, b@other.io\r\nDate: Tue, 15 Sep 2026 09:02:00 -0400\r\n"])
    assert set(gmail(conn).recipients_since(7, "America/Toronto")) == {
        "a@acme.ai", "b@other.io"}


def test_recipients_since_lowercases():
    conn = FakeIMAP(sent=[b"To: A@Acme.AI\r\nDate: Tue, 15 Sep 2026 09:02:00 -0400\r\n"])
    assert "a@acme.ai" in gmail(conn).recipients_since(7, "America/Toronto")


def test_a_missing_date_header_falls_back_to_today():
    assert mirror._header_date("", "America/Toronto")
    assert mirror._header_date("not a date", "America/Toronto")


def test_recipients_since_is_empty_when_the_folder_errors():
    class Broken(FakeIMAP):
        def select(self, mailbox, readonly=False):
            raise imaplib.IMAP4.error("no such folder")

    assert gmail(Broken()).recipients_since(7, "America/Toronto") == {}


# ------------------------------------------------------------------ push

class FakeDB:
    def __init__(self, to_mirror=(), queued=(), stale=()):
        self._to_mirror = list(to_mirror)
        self._queued = list(queued)
        self._stale = list(stale)
        self.mirrored = {}
        self.cleared = []
        self.marked = []
        self.sends = []
        self.logged = []

    def drafts_to_mirror(self, limit=50):
        return self._to_mirror[:limit]

    def queued_drafts(self, limit=100):
        return self._queued[:limit]

    def stale_mirrors(self):
        return self._stale

    def set_draft_mirror(self, draft_id, message_id):
        self.mirrored[draft_id] = message_id

    def clear_draft_mirror(self, draft_id):
        self.cleared.append(draft_id)

    def mark_draft(self, draft_id, status):
        self.marked.append((draft_id, status))

    def record_send(self, contact_id, email, subject, message_id, backend,
                    local_date, status="sent", error=""):
        self.sends.append((email, backend, local_date))

    def log(self, kind, ref="", detail=""):
        self.logged.append((kind, ref, detail))


def test_push_appends_and_records_the_message_id(tmp_path):
    conn = FakeIMAP()
    db = FakeDB(to_mirror=[draft(tmp_path)])
    report = mirror.MirrorReport()
    mirror._push(gmail(conn), settings(tmp_path), db, 50, report)
    assert report.pushed == ["a@acme.ai"]
    assert len(conn.appended) == 1
    assert db.mirrored[1].startswith("<")


def test_the_pushed_copy_carries_the_resume(tmp_path):
    conn = FakeIMAP()
    db = FakeDB(to_mirror=[draft(tmp_path)])
    mirror._push(gmail(conn), settings(tmp_path), db, 50, mirror.MirrorReport())
    assert b"resume.pdf" in conn.appended[0][2]


def test_the_copy_is_flagged_as_a_seen_draft(tmp_path):
    # Without \Draft, Gmail files it as an ordinary message rather than
    # something the compose window will open.
    conn = FakeIMAP()
    db = FakeDB(to_mirror=[draft(tmp_path)])
    mirror._push(gmail(conn), settings(tmp_path), db, 50, mirror.MirrorReport())
    assert "\\Draft" in conn.appended[0][1]


def test_an_unfinished_draft_never_reaches_the_drafts_folder(tmp_path):
    conn = FakeIMAP()
    db = FakeDB(to_mirror=[draft(tmp_path, body="I work on [YOUR FOCUS]\n")])
    report = mirror.MirrorReport()
    mirror._push(gmail(conn), settings(tmp_path), db, 50, report)
    assert conn.appended == []
    assert "unedited template" in report.skipped[0][1]


def test_a_draft_with_no_resume_never_reaches_the_drafts_folder(tmp_path):
    conn = FakeIMAP()
    db = FakeDB(to_mirror=[draft(tmp_path, attachments="")])
    report = mirror.MirrorReport()
    mirror._push(gmail(conn), settings(tmp_path), db, 50, report)
    assert conn.appended == []
    assert report.skipped[0][1] == "no resume attached"


def test_a_refused_append_is_not_recorded_as_mirrored(tmp_path):
    class Refuses(FakeIMAP):
        def append(self, *_a):
            return "NO", [b"over quota"]

    db = FakeDB(to_mirror=[draft(tmp_path)])
    report = mirror.MirrorReport()
    mirror._push(gmail(Refuses()), settings(tmp_path), db, 50, report)
    assert db.mirrored == {}
    assert report.skipped[0][1] == "IMAP refused the append"


# ------------------------------------------------------------- reconcile

def test_a_draft_sent_by_hand_is_recorded_and_dequeued(tmp_path):
    conn = FakeIMAP(sent=[b"To: a@acme.ai\r\nDate: Tue, 15 Sep 2026 09:02:00 -0400\r\n"])
    db = FakeDB(queued=[draft(tmp_path)])
    report = mirror.MirrorReport()
    mirror._reconcile(gmail(conn), settings(tmp_path), db, 7, report)
    assert report.reconciled == ["a@acme.ai"]
    assert db.marked == [(1, "sent")]
    assert db.sends == [("a@acme.ai", "gmail-manual", "2026-09-15")]


def test_a_manual_send_counts_against_the_day_it_happened(tmp_path):
    # Not the day it was noticed: the cap is a deliverability limit, so
    # backdating it to the real day is what keeps the ramp honest.
    conn = FakeIMAP(sent=[b"To: a@acme.ai\r\nDate: Mon, 14 Sep 2026 08:40:00 -0400\r\n"])
    db = FakeDB(queued=[draft(tmp_path)])
    mirror._reconcile(gmail(conn), settings(tmp_path), db, 7,
                      mirror.MirrorReport())
    assert db.sends[0][2] == "2026-09-14"


def test_a_queued_draft_nobody_has_written_to_is_left_alone(tmp_path):
    conn = FakeIMAP(sent=[b"To: someone@else.com\r\nDate: Tue, 15 Sep 2026 09:02:00 -0400\r\n"])
    db = FakeDB(queued=[draft(tmp_path)])
    report = mirror.MirrorReport()
    mirror._reconcile(gmail(conn), settings(tmp_path), db, 7, report)
    assert report.reconciled == [] and db.marked == []


def test_reconcile_does_nothing_with_an_empty_queue(tmp_path):
    conn = FakeIMAP(sent=[b"To: a@acme.ai\r\nDate: Tue, 15 Sep 2026 09:02:00 -0400\r\n"])
    db = FakeDB(queued=[])
    mirror._reconcile(gmail(conn), settings(tmp_path), db, 7,
                      mirror.MirrorReport())
    assert db.sends == []


# --------------------------------------------------------------- cleanup

def test_cleanup_drops_the_copy_of_a_draft_already_sent():
    conn = FakeIMAP(drafts=["<a@x>"])
    db = FakeDB(stale=[{"id": 3, "gmail_message_id": "<a@x>",
                        "email": "a@acme.ai"}])
    report = mirror.MirrorReport()
    mirror._cleanup(gmail(conn), db, report)
    assert report.dropped == ["a@acme.ai"]
    assert db.cleared == [3]


def test_a_copy_that_could_not_be_dropped_stays_recorded():
    # So the next sync tries again rather than losing track of it.
    class Broken(FakeIMAP):
        def select(self, mailbox, readonly=False):
            raise imaplib.IMAP4.error("reset")

    db = FakeDB(stale=[{"id": 3, "gmail_message_id": "<a@x>",
                        "email": "a@acme.ai"}])
    mirror._cleanup(gmail(Broken()), db, mirror.MirrorReport())
    assert db.cleared == []


def test_report_rows_cover_every_outcome():
    report = mirror.MirrorReport(pushed=["a@x"], dropped=["b@x"],
                                 reconciled=["c@x"], skipped=[("d@x", "why")])
    assert len(report.rows()) == 4
