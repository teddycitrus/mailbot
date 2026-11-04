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


# ---------------------------------------------------------- the growth limiter

def test_volume_at_most_doubles_day_over_day():
    """Coming off a hold, the earned cap is the ceiling, but a domain that
    sent 8 yesterday must not send 25 today. Double, then double again."""
    s = settings()
    cap, why = s.daily_cap(active_days=40, bounce_pct=0.0, recent_daily_max=8)
    assert cap == 16
    assert "double" in why
    assert s.daily_cap(40, 0.0, recent_daily_max=16)[0] == 25


def test_growth_limiter_never_cuts_below_the_start_value():
    """A quiet stretch (queue ran dry, sent 2/day) must not strangle the cap
    under the ramp's own opening step."""
    cap, _ = settings().daily_cap(active_days=40, bounce_pct=0.0,
                                  recent_daily_max=2)
    assert cap == 8


def test_growth_limiter_is_silent_in_normal_operation():
    """The ordinary +2 climb and the full ceiling never trip it."""
    s = settings()
    assert s.daily_cap(1, 0.0, recent_daily_max=8)[0] == 10
    assert s.daily_cap(99, 0.0, recent_daily_max=25)[0] == 25


def test_growth_limiter_skipped_without_history():
    cap, why = settings().daily_cap(active_days=40, bounce_pct=0.0,
                                    recent_daily_max=None)
    assert cap == 25
    assert "complete" in why


def test_the_hold_still_wins_over_the_growth_limiter():
    cap, why = settings().daily_cap(active_days=40, bounce_pct=9.0,
                                    recent_daily_max=8)
    assert cap == 8
    assert "held" in why


# --------------------------------------------------------- the ramp's inputs

@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "ramp.db")
    yield database
    database.close()


def _contact(db, email, verify="verified", provenance="guess"):
    """A contact in a class the gates still mail, so its bounce counts.

    recent_bounce_pct counts only recurrable bounces: those from addresses
    the send path would still allow today. The window-math tests below use
    this class so the arithmetic stays visible; the class rules themselves
    are tested further down.
    """
    cid = db.upsert_company(Company(name=email, domain=email.split("@")[1]))
    return db.upsert_contact(Contact(
        email=email, company_id=cid, verify_status=verify, provenance=provenance))


def _sent(db, email, day, verify="verified", provenance="guess"):
    db.record_send(_contact(db, email, verify, provenance),
                   email, "s", "mid", "smtp", day)


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


def test_max_daily_sends_reads_the_busiest_recent_send_day(db):
    for name in ("a", "b", "c"):
        _sent(db, f"{name}@x.com", "2026-09-08")
    _sent(db, "d@x.com", "2026-09-09")
    assert db.max_daily_sends(days=3) == 3
    assert db.max_daily_sends(days=1) == 1  # only the newest day


def test_max_daily_sends_before_a_day_excludes_it(db):
    _sent(db, "a@x.com", "2026-09-08")
    for name in ("b", "c"):
        _sent(db, f"{name}@x.com", "2026-09-09")
    assert db.max_daily_sends(before_local_date="2026-09-09") == 1


def test_max_daily_sends_is_zero_on_a_fresh_database(db):
    assert db.max_daily_sends() == 0


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


def test_bounce_pct_before_a_day_ignores_that_days_sends(db):
    """Today's clean sends must not dilute the rate and lift the hold mid-day."""
    _sent(db, "a@x.com", "2026-10-23")
    _sent(db, "b@x.com", "2026-10-23")
    db.conn.execute(
        "UPDATE contacts SET status = ? WHERE email = ?", (BOUNCED, "a@x.com")
    )
    db.conn.commit()
    for name in ("c", "d", "e", "f", "g", "h"):
        _sent(db, f"{name}@x.com", "2026-10-26")
    assert db.recent_bounce_pct(before_local_date="2026-10-26") == pytest.approx(50.0)
    assert db.recent_bounce_pct() == pytest.approx(12.5)


# ------------------------------------------- which bounces can recur at all

def _bounce(db, email):
    db.conn.execute(
        "UPDATE contacts SET status = ? WHERE email = ?", (BOUNCED, email))
    db.conn.commit()


def test_a_bounce_from_a_class_the_gates_refuse_does_not_hold(db):
    """site_unattributed catch-alls are refused by can_send_to today, so a
    bounce from one cannot happen again and must not keep the cap down.
    This exact class held the ramp at the floor for two weeks in October."""
    _sent(db, "a@x.com", "2026-09-08", verify="catchall",
          provenance="site_unattributed")
    for name in ("b", "c", "d"):
        _sent(db, f"{name}@x.com", "2026-09-08")
    _bounce(db, "a@x.com")
    assert db.recent_bounce_pct() == 0.0


def test_a_bounce_from_a_role_mailbox_does_not_hold(db):
    """Shared inboxes are refused outright now, whatever the probe said."""
    _sent(db, "hello@x.com", "2026-09-08", verify="verified")
    for name in ("b", "c", "d"):
        _sent(db, f"{name}@x.com", "2026-09-08")
    _bounce(db, "hello@x.com")
    assert db.recent_bounce_pct() == 0.0


def test_a_bounce_from_a_still_eligible_class_holds(db):
    """A verified guess or a trusted catch-all is still being sent to, so its
    bounce predicts more and must keep the brake on."""
    _sent(db, "a@x.com", "2026-09-08", verify="verified", provenance="guess")
    _sent(db, "b@y.com", "2026-09-08", verify="catchall",
          provenance="house_pattern")
    for name in ("c", "d"):
        _sent(db, f"{name}@x.com", "2026-09-08")
    _bounce(db, "a@x.com")
    _bounce(db, "b@y.com")
    assert db.recent_bounce_pct() == pytest.approx(50.0)


def test_discounted_bounces_still_fill_the_window_denominator(db):
    """The discount removes a bounce from the numerator only. The window
    stays the last N sends, so the maths of flushing stays unchanged."""
    _sent(db, "a@x.com", "2026-09-08", verify="catchall",
          provenance="site_unattributed")
    _sent(db, "b@x.com", "2026-09-08")
    _bounce(db, "a@x.com")
    _bounce(db, "b@x.com")
    assert db.recent_bounce_pct() == pytest.approx(50.0)


# ------------------------------------------------------ the health check

from src.doctor import FAIL, OK, WARN, _send_ramp  # noqa: E402


def _mail_day(db, day, n, bounced=0):
    """n verified personal sends on one day, the first `bounced` of them bounced."""
    cid = db.upsert_company(Company(name=f"Co {day}", domain=f"co-{day}.test"))
    for i in range(n):
        email = f"p{i}@co-{day}.test"
        contact = db.upsert_contact(Contact(
            email=email, company_id=cid, first_name="Ada",
            verify_status="verified", provenance="guess"))
        db.record_send(contact, email, "s", f"<{day}-{i}>", "smtp", day)
        if i < bounced:
            db.conn.execute("UPDATE contacts SET status=? WHERE id=?", (BOUNCED, contact))
    db.conn.commit()


def test_health_is_quiet_while_the_ramp_climbs(tmp_path):
    db = Database(tmp_path / "h.db")
    for day in ("2026-10-26", "2026-10-27", "2026-10-28"):
        _mail_day(db, day, 10)
    check = _send_ramp(settings(), db, "2026-10-29")
    assert check.state == OK and "held" not in check.detail


def test_one_held_day_is_a_warning(tmp_path):
    db = Database(tmp_path / "h.db")
    _mail_day(db, "2026-10-26", 10)
    _mail_day(db, "2026-10-27", 10, bounced=2)
    check = _send_ramp(settings(), db, "2026-10-28")
    assert check.state == WARN and "held at 8" in check.detail


def test_a_hold_three_send_days_long_is_a_failure(tmp_path):
    """October: pinned at 8 for two weeks while every morning said all good."""
    db = Database(tmp_path / "h.db")
    _mail_day(db, "2026-10-20", 10, bounced=2)
    for day in ("2026-10-21", "2026-10-22", "2026-10-23"):
        _mail_day(db, day, 8)
    check = _send_ramp(settings(), db, "2026-10-26")
    assert check.state == FAIL
    assert "4 send days" in check.detail


def test_ramp_off_is_fine(tmp_path):
    db = Database(tmp_path / "h.db")
    assert _send_ramp(settings(send_ramp_enabled=False), db, "2026-10-28").state == OK


# ------------------------------------------- a weekday with nothing sent

from src.doctor import _last_weekday_sent  # noqa: E402


def _queue_one(db):
    cid = db.upsert_company(Company(name="Q", domain="q.test"))
    contact = db.upsert_contact(Contact(email="ana@q.test", company_id=cid,
                                        first_name="Ana", verify_status="verified"))
    db.queue_draft(contact, "s", "b")


def test_a_silent_weekday_with_drafts_waiting_is_a_failure(tmp_path):
    db = Database(tmp_path / "h.db")
    _mail_day(db, "2026-10-29", 8)          # Thursday
    _queue_one(db)
    db.conn.execute("UPDATE drafts SET created_at = '2026-10-29T20:00:00+00:00'")
    db.conn.commit()
    check = _last_weekday_sent(db, "2026-11-02")   # Monday; Friday sent nothing
    assert check.state == FAIL and "2026-10-30" in check.detail


def test_the_weekend_is_skipped_when_looking_back(tmp_path):
    db = Database(tmp_path / "h.db")
    _mail_day(db, "2026-10-30", 8)          # Friday
    _queue_one(db)
    assert _last_weekday_sent(db, "2026-11-02").state == OK


def test_nothing_sent_and_nothing_queued_is_not_this_checks_business(tmp_path):
    db = Database(tmp_path / "h.db")
    assert _last_weekday_sent(db, "2026-11-02").state == OK


def test_a_queue_refilled_overnight_does_not_accuse_yesterday(tmp_path):
    db = Database(tmp_path / "h.db")
    _mail_day(db, "2026-10-29", 8)
    _queue_one(db)
    db.conn.execute("UPDATE drafts SET created_at = '2026-11-02T06:00:00+00:00'")
    db.conn.commit()
    assert _last_weekday_sent(db, "2026-11-02").state == OK


# ------------------------------------------------------------ intake

from src.doctor import _intake  # noqa: E402


def _intake_rows(db, n, day, named=True, verify="verified"):
    cid = db.upsert_company(Company(name=f"I {day}", domain=f"i-{day}.test"))
    for i in range(n):
        contact = db.upsert_contact(Contact(
            email=f"x{i}@i-{day}.test", company_id=cid,
            first_name="Ada" if named else "", verify_status=verify))
        db.conn.execute("UPDATE contacts SET created_at=? WHERE id=?",
                        (f"{day}T12:00:00+00:00", contact))
    db.conn.commit()


def test_a_week_of_healthy_intake_is_ok(tmp_path):
    db = Database(tmp_path / "h.db")
    _intake_rows(db, 80, "2026-10-30")
    assert _intake(settings(), db, "2026-11-03").state == OK


def test_a_trickle_below_one_days_sends_a_week_is_a_failure(tmp_path):
    """Oct 14-31: 2 to 11 sendable people a week arrived, and nothing said so."""
    db = Database(tmp_path / "h.db")
    _intake_rows(db, 6, "2026-10-30")
    _intake_rows(db, 50, "2026-10-30", named=False)        # nameless: not people
    _intake_rows(db, 50, "2026-10-30", verify="catchall")  # untrusted catch-all
    check = _intake(settings(), db, "2026-11-03")
    assert check.state == FAIL and "6 sendable" in check.detail


def test_thin_intake_is_a_warning(tmp_path):
    db = Database(tmp_path / "h.db")
    _intake_rows(db, 40, "2026-10-30")
    assert _intake(settings(), db, "2026-11-03").state == WARN


def test_only_the_last_seven_days_count(tmp_path):
    db = Database(tmp_path / "h.db")
    _intake_rows(db, 200, "2026-10-20")
    assert _intake(settings(), db, "2026-11-03").state == FAIL
