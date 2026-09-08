"""The duplicate-send guarantees are the thing most worth testing."""

from __future__ import annotations

import pytest

from src.database import Database
from src.models import BOUNCED, Company, Contact, REPLIED, SENT


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


@pytest.fixture
def company_id(db):
    return db.upsert_company(Company(name="Acme AI", domain="acme.ai"))


def test_company_domain_is_idempotent(db):
    first = db.upsert_company(Company(name="Acme AI", domain="acme.ai"))
    second = db.upsert_company(Company(name="Acme AI Rebrand", domain="acme.ai"))
    assert first == second
    assert db.stats()["companies"] == 1


def test_duplicate_contact_is_rejected(db, company_id):
    assert db.upsert_contact(Contact(email="a@acme.ai", company_id=company_id))
    assert db.upsert_contact(Contact(email="a@acme.ai", company_id=company_id)) is None
    assert db.stats()["contacts"] == 1


def test_email_is_normalised_to_lowercase(db, company_id):
    db.upsert_contact(Contact(email="Mixed@Acme.AI", company_id=company_id))
    assert db.contact_exists("mixed@acme.ai")
    assert db.upsert_contact(Contact(email="MIXED@ACME.AI")) is None


def test_fresh_address_is_sendable(db, company_id):
    db.upsert_contact(Contact(email="new@acme.ai", company_id=company_id))
    allowed, why = db.can_send_to("new@acme.ai")
    assert allowed and why == ""


@pytest.mark.parametrize("status", [SENT, REPLIED, BOUNCED])
def test_contacted_address_is_blocked(db, company_id, status):
    contact_id = db.upsert_contact(Contact(email="x@acme.ai", company_id=company_id))
    db.set_contact_status(contact_id, status)
    allowed, why = db.can_send_to("x@acme.ai")
    assert not allowed
    assert status in why


def test_suppression_blocks_sending(db, company_id):
    db.upsert_contact(Contact(email="stop@acme.ai", company_id=company_id))
    db.suppress("stop@acme.ai", "unsubscribed")
    allowed, why = db.can_send_to("stop@acme.ai")
    assert not allowed and why == "suppressed"


def test_send_log_blocks_a_second_send(db, company_id):
    contact_id = db.upsert_contact(Contact(email="once@acme.ai", company_id=company_id))
    db.record_send(contact_id, "once@acme.ai", "Hi", "<id@x>", "smtp", "2026-09-06")
    allowed, _ = db.can_send_to("once@acme.ai")
    assert not allowed


def test_send_log_survives_a_status_reset(db, company_id):
    """Even if a status is edited back, the send log still forbids a repeat."""
    contact_id = db.upsert_contact(Contact(email="once@acme.ai", company_id=company_id))
    db.record_send(contact_id, "once@acme.ai", "Hi", "<id@x>", "smtp", "2026-09-06")
    db.set_contact_status(contact_id, "pending")
    allowed, why = db.can_send_to("once@acme.ai")
    assert not allowed and why == "already in send log"


def test_daily_count_only_counts_successful_sends(db, company_id):
    a = db.upsert_contact(Contact(email="a@acme.ai", company_id=company_id))
    b = db.upsert_contact(Contact(email="b@acme.ai", company_id=company_id))
    db.record_send(a, "a@acme.ai", "s", "", "smtp", "2026-09-06", status="sent")
    db.record_send(b, "b@acme.ai", "s", "", "smtp", "2026-09-06", status="failed",
                   error="boom")
    assert db.sent_count_on("2026-09-06") == 1
    assert db.sent_count_on("2026-09-07") == 0


def test_one_person_per_company(db, company_id):
    first = db.upsert_contact(Contact(email="a@acme.ai", company_id=company_id))
    assert not db.company_has_contacted(company_id)
    db.queue_draft(first, "Subject", "Body")
    assert db.company_has_contacted(company_id)


def test_requeue_updates_rather_than_duplicates(db, company_id):
    contact_id = db.upsert_contact(Contact(email="a@acme.ai", company_id=company_id))
    db.queue_draft(contact_id, "First", "Body one")
    db.queue_draft(contact_id, "Second", "Body two")
    drafts = db.queued_drafts()
    assert len(drafts) == 1
    assert drafts[0]["subject"] == "Second"


def test_domain_cache_roundtrip(db):
    db.cache_domain("acme.ai", "mx.acme.ai", True)
    row = db.cached_domain("acme.ai")
    assert row["mx_host"] == "mx.acme.ai"
    assert bool(row["catchall"]) is True


# --------------------------------------------------------------- follow-ups

from datetime import datetime, timedelta, timezone  # noqa: E402

from src.models import SUPPRESSED  # noqa: E402


def _sent_days_ago(db, contact_id, email, days, subject="Subj", mid="<a@b>"):
    when = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    db.conn.execute(
        """INSERT INTO sends (contact_id, email, subject, message_id, backend,
                              status, error, sent_at, local_date)
           VALUES (?,?,?,?,'smtp','sent','',?,?)""",
        (contact_id, email, subject, mid, when, when[:10]),
    )
    db.conn.execute("UPDATE contacts SET status = ? WHERE id = ?", (SENT, contact_id))
    db.conn.commit()


def test_followup_due_after_the_waiting_period(db, company_id):
    cid = db.upsert_contact(Contact(email="a@acme.ai", company_id=company_id))
    _sent_days_ago(db, cid, "a@acme.ai", days=7)
    assert [r["email"] for r in db.due_for_followup(6, 2)] == ["a@acme.ai"]


def test_followup_not_due_before_the_waiting_period(db, company_id):
    cid = db.upsert_contact(Contact(email="a@acme.ai", company_id=company_id))
    _sent_days_ago(db, cid, "a@acme.ai", days=2)
    assert db.due_for_followup(6, 2) == []


def test_someone_who_replied_is_never_followed_up(db, company_id):
    cid = db.upsert_contact(Contact(email="a@acme.ai", company_id=company_id))
    _sent_days_ago(db, cid, "a@acme.ai", days=30)
    db.set_contact_status(cid, REPLIED)
    assert db.due_for_followup(6, 2) == []


def test_someone_who_opted_out_is_never_followed_up(db, company_id):
    cid = db.upsert_contact(Contact(email="a@acme.ai", company_id=company_id))
    _sent_days_ago(db, cid, "a@acme.ai", days=30)
    db.suppress("a@acme.ai", "opt-out reply")
    assert db.due_for_followup(6, 2) == []


def test_only_one_followup_then_stop(db, company_id):
    """Cap is total sends: original plus one bump, then never again."""
    cid = db.upsert_contact(Contact(email="a@acme.ai", company_id=company_id))
    _sent_days_ago(db, cid, "a@acme.ai", days=20)
    assert len(db.due_for_followup(6, 2)) == 1
    _sent_days_ago(db, cid, "a@acme.ai", days=10)   # the bump
    assert db.due_for_followup(6, 2) == [], "must not chase a third time"


def test_followup_carries_the_original_thread_id(db, company_id):
    cid = db.upsert_contact(Contact(email="a@acme.ai", company_id=company_id))
    _sent_days_ago(db, cid, "a@acme.ai", days=9, subject="Hi Acme", mid="<orig@x>")
    row = db.due_for_followup(6, 2)[0]
    assert row["first_message_id"] == "<orig@x>"
    assert row["first_subject"] == "Hi Acme"


def test_can_send_to_again_gate(db, company_id):
    cid = db.upsert_contact(Contact(email="a@acme.ai", company_id=company_id))
    assert db.can_send_to_again("a@acme.ai")[0] is False, "not contacted yet"
    _sent_days_ago(db, cid, "a@acme.ai", days=9)
    assert db.can_send_to_again("a@acme.ai")[0] is True
    db.set_contact_status(cid, REPLIED)
    assert db.can_send_to_again("a@acme.ai")[0] is False
    db.set_contact_status(cid, SENT)
    db.suppress("a@acme.ai", "opt-out")
    assert db.can_send_to_again("a@acme.ai")[0] is False


def test_unknown_address_cannot_be_followed_up(db):
    assert db.can_send_to_again("nobody@nowhere.ai") == (False, "unknown contact")
