"""The end-of-window check on a single day's bounce rate.

The warmup ramp already brakes on bounces, but it reads the last hundred
sends. That horizon is right for sender reputation and useless for noticing
that today went wrong: two bounces in twenty-one sends is 9.5% for the day and
moves a hundred-send average by two points, so the brake stays off and nothing
says a word. This is the check that looks at one day on its own.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.alerts import bounce_check
from src.database import Database
from src.models import BOUNCED, Company, Contact

DAY = "2026-09-21"


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


def settings(pct=15, floor=3):
    return SimpleNamespace(
        daily_bounce_alert_pct=pct,
        daily_bounce_alert_min_sends=floor,
        send_timezone="America/Toronto",
    )


def send(db, email, day=DAY, bounced=False):
    company = db.upsert_company(Company(name="Acme", domain=email.split("@")[1]))
    contact = db.upsert_contact(Contact(email=email, company_id=company,
                                        confidence=75))
    db.record_send(contact, email, "Hi", "<id>", "smtp", day)
    if bounced:
        db.set_contact_status(contact, BOUNCED)


def test_a_clean_day_says_nothing(db):
    for i in range(10):
        send(db, f"ok{i}@acme.ai")
    report = bounce_check(settings(), db, DAY, notify=False)
    assert report.pct == 0.0 and not report.over


def test_the_real_day_that_prompted_this_stays_under(db):
    """Two of twenty-one is 9.5%: bad, worth seeing in the digest, not an alarm."""
    for i in range(19):
        send(db, f"ok{i}@acme.ai")
    send(db, "dead1@acme.ai", bounced=True)
    send(db, "dead2@acme.ai", bounced=True)
    report = bounce_check(settings(), db, DAY, notify=False)
    assert round(report.pct, 1) == 9.5
    assert not report.over


def test_a_day_over_the_line_is_flagged(db):
    for i in range(6):
        send(db, f"ok{i}@acme.ai")
    for i in range(4):
        send(db, f"dead{i}@acme.ai", bounced=True)
    report = bounce_check(settings(), db, DAY, notify=False)
    assert report.pct == 40.0 and report.over
    assert {r["email"] for r in report.bounced} == {
        f"dead{i}@acme.ai" for i in range(4)}


def test_a_day_too_small_to_judge_is_left_alone(db):
    """One bounce out of two is 50% and means nothing at all."""
    send(db, "ok@acme.ai")
    send(db, "dead@acme.ai", bounced=True)
    report = bounce_check(settings(), db, DAY, notify=False)
    assert report.pct == 50.0
    assert report.too_few and not report.over


def test_the_floor_can_be_lowered(db):
    send(db, "ok@acme.ai")
    send(db, "dead@acme.ai", bounced=True)
    report = bounce_check(settings(floor=2), db, DAY, notify=False)
    assert report.over


def test_the_threshold_is_configurable(db):
    for i in range(9):
        send(db, f"ok{i}@acme.ai")
    send(db, "dead@acme.ai", bounced=True)
    assert not bounce_check(settings(pct=15), db, DAY, notify=False).over
    assert bounce_check(settings(pct=5), db, DAY, notify=False).over


def test_exactly_at_the_threshold_does_not_alarm(db):
    """Over, not at. A rate equal to the limit is the limit being respected."""
    for i in range(17):
        send(db, f"ok{i}@acme.ai")
    for i in range(3):
        send(db, f"dead{i}@acme.ai", bounced=True)
    report = bounce_check(settings(pct=15), db, DAY, notify=False)
    assert report.pct == 15.0 and not report.over


def test_another_days_bounces_do_not_count_against_this_one(db):
    """Attribution is by the day the message went out, not the day it failed."""
    for i in range(10):
        send(db, f"ok{i}@acme.ai", day=DAY)
    for i in range(4):
        send(db, f"dead{i}@acme.ai", day="2026-09-18", bounced=True)
    assert not bounce_check(settings(), db, DAY, notify=False).over
    assert bounce_check(settings(), db, "2026-09-18", notify=False).over


def test_the_alert_names_what_the_address_was_believed_to_be_worth(db):
    """The verdict is the diagnosis: 'no SMTP answer' means never proved."""
    for i in range(6):
        send(db, f"ok{i}@acme.ai")
    company = db.upsert_company(Company(name="Dead", domain="dead.ai"))
    contact = db.upsert_contact(Contact(
        email="gone@dead.ai", company_id=company, confidence=75,
        verify_detail="no SMTP answer (0); trusting published source"))
    db.record_send(contact, "gone@dead.ai", "Hi", "<id>", "smtp", DAY)
    db.set_contact_status(contact, BOUNCED)

    report = bounce_check(settings(pct=5), db, DAY, notify=False)
    assert report.over
    row = report.bounced[0]
    assert row["confidence"] == 75
    assert "no SMTP answer" in row["verify_detail"]
