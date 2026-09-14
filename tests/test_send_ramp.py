"""The warmup ramp on the daily send cap.

DAILY_SEND_LIMIT is the ceiling the cap climbs to, not the volume sent on the
first day. What matters is that the climb is driven by days actually sent on,
that it never overshoots the ceiling, and that it gives ground when mail starts
bouncing.
"""

from __future__ import annotations

import pytest

from src.config import Settings
from src.database import Database
from src.models import BOUNCED, Company, Contact


class _Stub:
    """Carries the ramp fields and borrows the real method under test.

    Settings has a great many required fields and daily_cap reads five of
    them, so stubbing keeps each test's dependencies visible.
    """
    daily_cap = Settings.daily_cap

    def __init__(self, **fields):
        self.__dict__.update(fields)


def settings(**over):
    base = dict(
        daily_send_limit=25,
        send_ramp_enabled=True,
        send_ramp_start=8,
        send_ramp_step=2,
        send_ramp_max_bounce_pct=5,
    )
    base.update(over)
    return _Stub(**base)


# ------------------------------------------------------------- the ramp itself

def test_opens_at_the_start_value_not_the_ceiling():
    cap, why = settings().daily_cap(active_days=0, bounce_pct=0.0)
    assert cap == 8
    assert "ramp day 1" in why


def test_climbs_one_step_per_day_sent_on():
    s = settings()
    assert s.daily_cap(1, 0.0)[0] == 10
    assert s.daily_cap(2, 0.0)[0] == 12
    assert s.daily_cap(5, 0.0)[0] == 18


def test_never_exceeds_the_ceiling():
    s = settings()
    cap, why = s.daily_cap(active_days=999, bounce_pct=0.0)
    assert cap == 25
    assert "complete" in why


def test_ramp_can_be_switched_off():
    cap, why = settings(send_ramp_enabled=False).daily_cap(0, 0.0)
    assert cap == 25
    assert why == "ramp off"


def test_a_ceiling_below_the_start_value_still_wins():
    """DAILY_SEND_LIMIT is a hard ceiling even if it is set under the start."""
    cap, _ = settings(daily_send_limit=5).daily_cap(active_days=0, bounce_pct=0.0)
    assert cap == 5


# ------------------------------------------------------------ the bounce brake

def test_bouncing_holds_the_cap_back_at_the_start_value():
    cap, why = settings().daily_cap(active_days=20, bounce_pct=9.0)
    assert cap == 8
    assert "9.0%" in why


def test_bounce_rate_on_the_threshold_does_not_trip_the_brake():
    cap, _ = settings().daily_cap(active_days=20, bounce_pct=5.0)
    assert cap == 25


# --------------------------------------------------------- the ramp's inputs

@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "ramp.db")
    yield database
    database.close()


def _contact(db, email):
    cid = db.upsert_company(Company(name=email, domain=email.split("@")[1]))
    return db.upsert_contact(Contact(email=email, company_id=cid))


def _sent(db, email, day):
    db.record_send(_contact(db, email), email, "s", "mid", "smtp", day)


def test_active_send_days_counts_distinct_days(db):
    _sent(db, "a@x.com", "2026-09-08")
    _sent(db, "b@x.com", "2026-09-08")
    _sent(db, "c@x.com", "2026-09-09")
    assert db.active_send_days("2026-09-12") == 2


def test_active_send_days_excludes_today(db):
    """Otherwise today's cap would climb the moment the first message went out."""
    _sent(db, "a@x.com", "2026-09-08")
    _sent(db, "b@x.com", "2026-09-12")
    assert db.active_send_days("2026-09-12") == 1


def test_active_send_days_is_zero_on_a_fresh_database(db):
    assert db.active_send_days("2026-09-12") == 0


def test_bounce_pct_is_zero_with_nothing_sent(db):
    assert db.recent_bounce_pct() == 0.0


def test_bounce_pct_counts_contacts_the_inbox_marked_bounced(db):
    for name in ("a", "b", "c", "d"):
        _sent(db, f"{name}@x.com", "2026-09-08")
    db.conn.execute(
        "UPDATE contacts SET status = ? WHERE email = ?", (BOUNCED, "a@x.com")
    )
    db.conn.commit()
    assert db.recent_bounce_pct() == pytest.approx(25.0)
