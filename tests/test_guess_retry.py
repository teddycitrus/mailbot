"""A guess that hard-bounces earns its person one retry at the next convention.

The bounce proves one convention wrong at that company, so the retry skips
it, and colleagues still waiting on it are pulled before they bounce too.
Two addresses per person at most. All addresses here are synthetic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.database import Database
from src.main import Pipeline
from src.models import (
    BOUNCED, CATCHALL, Company, Contact, PENDING, PROV_GUESS, PROV_GUESS_RETRY,
    QUEUED, SUPPRESSED, VOLUME_LANE,
)


def guess(db, cid, email, first, last, status=PENDING, prov=PROV_GUESS):
    return db.upsert_contact(Contact(
        email=email, company_id=cid, first_name=first, last_name=last,
        verify_status=CATCHALL, provenance=prov, lane=VOLUME_LANE,
        status=status, confidence=40))


def hard_bounce(db, email, days_ago=3):
    db.suppress(email, "hard bounce")
    db.conn.execute("UPDATE contacts SET status = ? WHERE email = ?", (BOUNCED, email))
    when = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")
    db.conn.execute("UPDATE suppression SET created_at = ? WHERE email = ?", (when, email))
    db.conn.commit()


@pytest.fixture
def setup(tmp_path):
    db = Database(tmp_path / "o.db")
    cid = db.upsert_company(Company(name="Acme", domain="acme.test", location="Toronto"))
    bot = object.__new__(Pipeline)
    bot.db = db
    bot.settings = SimpleNamespace(volume_guesses=True, guess_retry_after_days=2)
    yield db, cid, bot
    db.close()


def test_a_bounced_guess_gets_the_next_convention(setup):
    db, cid, bot = setup
    guess(db, cid, "ann@acme.test", "Ann", "Lee")
    hard_bounce(db, "ann@acme.test")
    assert bot.retry_guesses() == 1
    retry = db.find_contact_by_email("ann.lee@acme.test")
    assert (retry["provenance"], retry["lane"], retry["status"]) == (
        PROV_GUESS_RETRY, VOLUME_LANE, PENDING)


def test_a_colleague_on_the_same_convention_is_pulled_and_reguessed(setup):
    db, cid, bot = setup
    guess(db, cid, "ann@acme.test", "Ann", "Lee")
    bo = guess(db, cid, "bo@acme.test", "Bo", "Ruiz", status=QUEUED)
    db.queue_draft(bo, "Hi", "Hello")
    hard_bounce(db, "ann@acme.test")
    assert bot.retry_guesses() == 2
    assert db.find_contact_by_email("bo@acme.test")["status"] == SUPPRESSED
    assert db.conn.execute("SELECT status FROM drafts WHERE contact_id = ?",
                           (bo,)).fetchone()[0] == "skipped"
    assert db.find_contact_by_email("bo.ruiz@acme.test") is not None
    # Pulled, not opted out: nothing lands on the suppression list.
    assert not db.is_suppressed("bo@acme.test")


def test_one_retry_per_person_and_a_rerun_adds_nothing(setup):
    db, cid, bot = setup
    guess(db, cid, "ann@acme.test", "Ann", "Lee")
    hard_bounce(db, "ann@acme.test")
    bot.retry_guesses()
    assert bot.retry_guesses() == 0
    hard_bounce(db, "ann.lee@acme.test")
    assert bot.retry_guesses() == 0
    assert db.conn.execute(
        "SELECT COUNT(*) FROM contacts WHERE first_name = 'Ann'").fetchone()[0] == 2


def test_a_fresh_bounce_waits_out_the_delay(setup):
    db, cid, bot = setup
    guess(db, cid, "ann@acme.test", "Ann", "Lee")
    hard_bounce(db, "ann@acme.test", days_ago=0)
    assert bot.retry_guesses() == 0
    assert bot.retry_guesses(after_days=0) == 1


def test_a_soft_bounce_is_not_a_disproved_convention(setup):
    db, cid, bot = setup
    guess(db, cid, "ann@acme.test", "Ann", "Lee")
    hard_bounce(db, "ann@acme.test")
    db.conn.execute("UPDATE suppression SET reason = 'soft bounce'")
    db.conn.commit()
    assert bot.retry_guesses() == 0


def test_off_without_volume_guesses(setup):
    db, cid, bot = setup
    guess(db, cid, "ann@acme.test", "Ann", "Lee")
    hard_bounce(db, "ann@acme.test")
    bot.settings.volume_guesses = False
    assert bot.retry_guesses() == 0


def test_the_bounce_closes_the_company_to_all_but_retries(setup):
    db, cid, _bot = setup
    guess(db, cid, "ann@acme.test", "Ann", "Lee")
    hard_bounce(db, "ann@acme.test")
    assert db.company_closed(cid) == "a colleague's hard bounce"
    assert db.company_closed(cid, PROV_GUESS) == "a colleague's hard bounce"
    assert db.company_closed(cid, PROV_GUESS_RETRY) == ""
    # The bounced guess does not count against the per-company cap for it.
    assert db.company_slot(cid, 1, PROV_GUESS_RETRY) == (True, "")
    # A reply still closes it to everyone.
    db.conn.execute("INSERT INTO contacts (company_id, email, status, created_at) "
                    "VALUES (?, 'cy@acme.test', 'replied', '2026-01-01')", (cid,))
    db.conn.commit()
    assert db.company_closed(cid, PROV_GUESS_RETRY) == "someone there already replied"
