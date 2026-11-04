"""At most once: a send whose record could not be written is never repeated.

SMTP accepts the message, then the database write that records it fails
(disk full, a lock held past the timeout). The draft is still queued and the
contact still looks unmailed, so the next tick sends the same email again,
and the one after that. The record of an attempt has to exist before the
message leaves.

All addresses here are synthetic.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, time as dtime, timedelta, timezone
from email.utils import parseaddr
from types import SimpleNamespace

from src import pipeline as pipeline_module
from src.database import Database
from src.main import Pipeline
from src.models import SENT, Company, Contact, SendResult

LINK = "https://drive.google.com/file/d/abc/view"


class FlakyConn:
    """The real connection, until `broken` is set: then every write fails."""

    def __init__(self, conn):
        self._conn = conn
        self.broken = False

    def execute(self, sql, *args):
        if self.broken and sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            raise sqlite3.OperationalError("database or disk is full")
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class Backend:
    """Accepts every message, then breaks the database, as a full disk would."""

    name = "recorder"

    def __init__(self, conn: FlakyConn | None = None):
        self.sent: list = []
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def send(self, msg):
        self.sent.append(parseaddr(msg["To"])[1])
        if self.conn is not None:
            self.conn.broken = True
        return SendResult(True, message_id=f"<{len(self.sent)}@x>", backend=self.name)


def _bot(tmp_path, followup_template=None):
    obj = object.__new__(Pipeline)
    path = tmp_path / "followup.txt"
    path.write_text(followup_template or
                    "Subject: Re: {original_subject}\n\nHi {first_name}, bumping this.\n",
                    encoding="utf-8")
    obj.settings = SimpleNamespace(
        from_email="j@x.test", from_name="J", reply_to="", unsubscribe_mailto="j@x.test",
        send_delay_seconds=0, resume_path=str(tmp_path / "none.pdf"), resume_link=LINK,
        send_timezone="America/Toronto", send_window_start=dtime(0, 0),
        send_window_end=dtime(23, 59), per_recipient_timezone=False,
        send_ramp_enabled=False, daily_send_limit=25, dry_run=False,
        require_for_send=lambda: None, mirror_to_drafts=False,
        followup_enabled=True, followup_template_path=path,
        followup_after_days=6, followup_max_total_sends=2, smtp_host="smtp.test",
    )
    obj.db = Database(tmp_path / "once.db")
    flaky = FlakyConn(obj.db.conn)
    obj.db.conn = flaky
    cid = obj.db.upsert_company(Company(name="Acme", domain="acme.test"))
    contact = obj.db.upsert_contact(Contact(
        email="ana@acme.test", first_name="Ana", company_id=cid, verify_status="verified"))
    return obj, flaky, contact


def test_a_first_email_whose_record_failed_is_not_sent_again(tmp_path):
    bot, flaky, contact = _bot(tmp_path)
    bot.db.queue_draft(contact, "Subj", f"Hi Ana\n[my resume]({LINK})\n")
    first = Backend(flaky)
    try:
        bot._deliver(first, bot.db.queued_drafts(), "2026-11-04")
    except sqlite3.OperationalError:
        pass
    assert first.sent == ["ana@acme.test"]

    flaky.broken = False
    second = Backend()
    bot._deliver(second, bot.db.queued_drafts(), "2026-11-04")
    assert second.sent == [], "mailed the same person twice"


def test_a_followup_whose_record_failed_is_not_sent_again(tmp_path, monkeypatch):
    bot, flaky, contact = _bot(tmp_path)
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    bot.db.conn.execute(
        """INSERT INTO sends (contact_id, email, subject, message_id, backend,
           status, error, sent_at, local_date) VALUES (?,?,?,?,?,'sent','',?,?)""",
        (contact, "ana@acme.test", "Subj", "<0@x>", "smtp", week_ago, week_ago[:10]))
    bot.db.conn.execute("UPDATE contacts SET status=? WHERE id=?", (SENT, contact))
    bot.db.conn.commit()

    first = Backend(flaky)
    monkeypatch.setattr(pipeline_module, "make_backend", lambda s: first)
    try:
        bot.followup(limit=1)
    except sqlite3.OperationalError:
        pass
    assert first.sent == ["ana@acme.test"]

    flaky.broken = False
    second = Backend()
    monkeypatch.setattr(pipeline_module, "make_backend", lambda s: second)
    bot.followup(limit=1)
    assert second.sent == [], "bumped the same person twice"


def test_a_normal_send_is_recorded_as_sent(tmp_path):
    bot, flaky, contact = _bot(tmp_path)
    bot.db.queue_draft(contact, "Subj", f"Hi Ana\n[my resume]({LINK})\n")
    assert bot._deliver(Backend(), bot.db.queued_drafts(), "2026-11-04") == 1
    row = bot.db.conn.execute("SELECT status, message_id FROM sends").fetchone()
    assert tuple(row) == ("sent", "<1@x>")
    assert bot.db.find_contact_by_email("ana@acme.test")["status"] == SENT
    assert bot.db.sent_count_on("2026-11-04") == 1


def test_a_refused_send_is_recorded_as_failed_and_can_be_retried(tmp_path):
    bot, flaky, contact = _bot(tmp_path)
    bot.db.queue_draft(contact, "Subj", f"Hi Ana\n[my resume]({LINK})\n")

    class Refuses(Backend):
        def send(self, msg):
            self.sent.append(parseaddr(msg["To"])[1])
            return SendResult(False, error="451 try later", backend=self.name)

    assert bot._deliver(Refuses(), bot.db.queued_drafts(), "2026-11-04") == 0
    row = bot.db.conn.execute("SELECT status FROM sends").fetchone()
    assert row["status"] == "failed"
    ok, _ = bot.db.can_send_to("ana@acme.test")
    assert ok, "a refused attempt must not count as a delivery"


def test_a_stuck_attempt_counts_against_the_day_and_the_company(tmp_path):
    bot, flaky, contact = _bot(tmp_path)
    attempt = bot.db.begin_send(contact, "ana@acme.test", "Subj", "2026-11-04")
    assert attempt
    assert bot.db.sent_count_on("2026-11-04") == 1
    company = bot.db.find_contact_by_email("ana@acme.test")["company_id"]
    assert company in bot.db.companies_sent_on("2026-11-04")
    ok, why = bot.db.can_send_to("ana@acme.test")
    assert not ok and "send log" in why
