"""Two sending accounts over one database.

Each lane has its own cap, ramp and bounce brake, read only from its own
sends. Who may be mailed stays global: one send by either account closes a
person for both. Guesses on catch-all domains are reserved for the volume
lane and invisible to main. All addresses here are synthetic.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, time as dtime, timezone
from types import SimpleNamespace

import pytest

from src import alerts, pipeline as pipeline_module
from src.config import ConfigError, Settings
from src.database import Database
from src.emailer import WindowState, local_date, window_state
from src.lanes import guess_hold, spread
from src.main import Pipeline, main
from src.models import (
    BOUNCED, CATCHALL, Company, Contact, PENDING, PROV_GUESS, VERIFIED,
    VOLUME_LANE, lane_deliverable,
)

TODAY = local_date("America/Toronto")
LINK = "[my resume](https://drive.google.com/file/d/abc/view)"


def add(db, email, verify=VERIFIED, prov="", lane="", company=None):
    domain = email.split("@")[1]
    cid = db.upsert_company(Company(name=company or domain, domain=domain,
                                    location="Toronto"))
    return db.upsert_contact(Contact(
        email=email, company_id=cid, first_name="Ana", verify_status=verify,
        provenance=prov, lane=lane, status=PENDING, confidence=90))


def send(db, contact_id, email, ok=True, day=TODAY):
    attempt = db.begin_send(contact_id, email, "Hi", day)
    db.finish_send(attempt, contact_id, "<m@x>", "smtp", ok)


@pytest.fixture
def lanes(tmp_path):
    main_db = Database(tmp_path / "o.db")
    volume_db = Database(tmp_path / "o.db", VOLUME_LANE)
    yield main_db, volume_db
    main_db.close()
    volume_db.close()


# ------------------------------------------------------------------ the gate

@pytest.mark.parametrize("verify, prov, reserved, lane, ok", [
    (VERIFIED, "", "", "main", True),
    (VERIFIED, "", "", VOLUME_LANE, True),
    (CATCHALL, "founder_published", "", VOLUME_LANE, True),
    (CATCHALL, PROV_GUESS, VOLUME_LANE, VOLUME_LANE, True),
    # A guess is never main's, reserved or not.
    (CATCHALL, PROV_GUESS, VOLUME_LANE, "main", False),
    (CATCHALL, PROV_GUESS, "", "main", False),
    # Unreserved, a guess is not the volume lane's either.
    (CATCHALL, PROV_GUESS, "", VOLUME_LANE, False),
    # A verified contact reserved for the volume lane stays off main.
    (VERIFIED, "", VOLUME_LANE, "main", False),
    # A published site address reserved for it is the volume lane's too.
    (CATCHALL, "site_unattributed", VOLUME_LANE, VOLUME_LANE, True),
    (CATCHALL, "site_unattributed", VOLUME_LANE, "main", False),
])
def test_lane_deliverable(verify, prov, reserved, lane, ok):
    assert lane_deliverable(verify, prov, reserved, lane) is ok


def test_a_reserved_guess_is_refused_on_main_and_allowed_on_volume(lanes):
    main_db, volume_db = lanes
    add(main_db, "ann@guess.test", CATCHALL, PROV_GUESS, VOLUME_LANE)
    allowed, why = main_db.can_send_to("ann@guess.test")
    assert not allowed and why == "reserved for the volume lane"
    assert volume_db.can_send_to("ann@guess.test") == (True, "")
    # Drafting runs on main but may ask on the volume lane's behalf.
    assert main_db.can_send_to("ann@guess.test", VOLUME_LANE) == (True, "")


def test_a_send_from_either_lane_closes_the_person_for_both(lanes):
    main_db, volume_db = lanes
    cid = add(main_db, "bo@open.test")
    send(volume_db, cid, "bo@open.test")
    assert main_db.can_send_to("bo@open.test") == (False, "already sent")
    assert volume_db.can_send_to("bo@open.test") == (False, "already sent")


def test_main_never_sees_a_reserved_draft_and_never_mirrors_one(lanes):
    main_db, volume_db = lanes
    a = add(main_db, "a@open.test")
    g = add(main_db, "g@guess.test", CATCHALL, PROV_GUESS, VOLUME_LANE)
    main_db.queue_draft(a, "Hi", LINK)
    main_db.queue_draft(g, "Hi", LINK)
    assert [d["email"] for d in main_db.queued_drafts(limit=None)] == ["a@open.test"]
    assert {d["email"] for d in volume_db.queued_drafts(limit=None)} == {
        "a@open.test", "g@guess.test"}
    assert [d["email"] for d in main_db.drafts_to_mirror()] == ["a@open.test"]
    assert main_db.queued_draft_count(reserved=False) == 1
    assert main_db.queued_draft_count(reserved=True) == 1
    assert main_db.queued_draft_count() == 2


# ----------------------------------------------------- per-lane accounting

def test_each_lane_counts_only_its_own_sends(lanes):
    main_db, volume_db = lanes
    send(main_db, add(main_db, "m@one.test"), "m@one.test")
    send(volume_db, add(main_db, "v1@two.test"), "v1@two.test")
    send(volume_db, add(main_db, "v2@three.test"), "v2@three.test")
    assert main_db.sent_count_on(TODAY) == 1
    assert volume_db.sent_count_on(TODAY) == 2
    assert [r["email"] for r in volume_db.sends_on(TODAY)] == ["v1@two.test", "v2@three.test"]


def test_a_guess_bounce_counts_against_the_volume_lane_only(lanes):
    main_db, volume_db = lanes
    g = add(main_db, "g@guess.test", CATCHALL, PROV_GUESS, VOLUME_LANE)
    m = add(main_db, "m@open.test")
    send(volume_db, g, "g@guess.test", day="2026-01-05")
    send(main_db, m, "m@open.test", day="2026-01-05")
    main_db.conn.execute("UPDATE contacts SET status = ? WHERE id = ?", (BOUNCED, g))
    main_db.conn.commit()
    assert volume_db.recent_bounce_pct() == 100.0
    assert main_db.recent_bounce_pct() == 0.0
    sent, bounced = volume_db.bounces_on("2026-01-05")
    assert sent == 1 and [r["email"] for r in bounced] == ["g@guess.test"]
    assert main_db.bounces_on("2026-01-05")[1] == []


def test_a_bump_goes_from_the_lane_that_wrote_first(lanes):
    main_db, volume_db = lanes
    cid = add(main_db, "a@open.test")
    send(volume_db, cid, "a@open.test", day="2026-01-05")
    assert [r["email"] for r in volume_db.due_for_followup(-1, 2)] == ["a@open.test"]
    assert main_db.due_for_followup(-1, 2) == []
    assert main_db.can_send_to_again("a@open.test") == (
        False, "first mailed from the volume lane")
    assert volume_db.can_send_to_again("a@open.test") == (True, "")


def test_followups_are_counted_apart_from_first_emails(lanes):
    main_db, _ = lanes
    cid = add(main_db, "a@open.test")
    send(main_db, cid, "a@open.test")
    send(main_db, cid, "a@open.test")
    assert main_db.sent_count_on(TODAY) == 2
    assert main_db.followup_count_on(TODAY) == 1


def test_another_lane_can_tell_when_main_is_full(lanes):
    main_db, volume_db = lanes
    assert volume_db.lane_at_cap("main", TODAY) == (False, "main has not run today")
    main_db.record_cap(TODAY, 1)
    assert volume_db.lane_at_cap("main", TODAY) == (False, "main at 0/1")
    send(main_db, add(main_db, "m@one.test"), "m@one.test")
    assert volume_db.lane_at_cap("main", TODAY) == (True, "main at 1/1")


def test_an_old_database_gains_lanes_and_its_history_is_mains(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE contacts (id INTEGER PRIMARY KEY, company_id INTEGER,
            email TEXT NOT NULL, first_name TEXT DEFAULT '', last_name TEXT DEFAULT '',
            role TEXT DEFAULT '', bio TEXT DEFAULT '', linkedin TEXT DEFAULT '',
            confidence INTEGER DEFAULT 0, source TEXT DEFAULT 'scraped',
            verify_status TEXT DEFAULT 'unverified', verify_detail TEXT DEFAULT '',
            status TEXT DEFAULT 'pending', created_at TEXT NOT NULL,
            provenance TEXT DEFAULT '');
        CREATE TABLE sends (id INTEGER PRIMARY KEY, contact_id INTEGER NOT NULL,
            email TEXT NOT NULL, subject TEXT DEFAULT '', message_id TEXT DEFAULT '',
            backend TEXT DEFAULT '', status TEXT DEFAULT 'sent', error TEXT DEFAULT '',
            sent_at TEXT NOT NULL, local_date TEXT NOT NULL);
        INSERT INTO contacts (email, status, verify_status, created_at)
            VALUES ('old@x.test', 'sent', 'verified', '2026-01-01');
        INSERT INTO sends (contact_id, email, sent_at, local_date)
            VALUES (1, 'old@x.test', '2026-01-01T15:00:00+00:00', '2026-01-01');
    """)
    conn.commit()
    conn.close()
    db = Database(path)
    assert db.sent_count_on("2026-01-01") == 1
    assert Database(path, VOLUME_LANE).sent_count_on("2026-01-01") == 0
    assert db.find_contact_by_email("old@x.test")["lane"] == ""
    db.close()


# -------------------------------------------------------- spreading the day

def st(phase, zone="America/Toronto", ticks=0):
    return WindowState(phase, zone, zone, ticks)


def test_an_open_window_gets_its_share_divided_over_the_ticks_left():
    rows = [(f"d{i}", st("open", ticks=9)) for i in range(18)]
    assert len(spread(rows, 18)) == 2


def test_the_last_tick_of_a_window_sends_everything_still_owed():
    rows = [(f"d{i}", st("open", ticks=1)) for i in range(18)]
    assert len(spread(rows, 7)) == 7


def test_a_zone_still_to_open_keeps_its_share():
    east = [(f"e{i}", st("open", ticks=9)) for i in range(3)]
    west = [(f"w{i}", st("later", "America/Los_Angeles")) for i in range(51)]
    assert spread(east + west, 16) == ["e0"]


def test_a_closed_zone_hands_its_share_to_the_open_one():
    east = [(f"e{i}", st("open", ticks=9)) for i in range(3)]
    west = [(f"w{i}", st("closed", "America/Los_Angeles")) for i in range(51)]
    assert spread(east + west, 16) == ["e0", "e1"]


def test_spread_never_exceeds_the_allowance_and_keeps_priority_order():
    rows = [(f"d{i}", st("open", ticks=1)) for i in range(5)]
    assert spread(rows, 3) == ["d0", "d1", "d2"]
    assert spread(rows, 0) == []


def test_window_state_reads_the_recipients_own_clock():
    start, end = dtime(8, 30), dtime(10, 30)
    # Wednesday 13:30 UTC: 08:30 in Toronto, 05:30 in San Francisco.
    now = datetime(2026, 11, 4, 13, 30, tzinfo=timezone.utc)
    toronto = window_state("Toronto", "America/Toronto", start, end, 15, now)
    assert toronto.open and toronto.ticks_left == 9
    sf = window_state("San Francisco", "America/Toronto", start, end, 15, now)
    assert sf.phase == "later" and sf.today
    late = datetime(2026, 11, 4, 16, 0, tzinfo=timezone.utc)
    assert window_state("Toronto", "America/Toronto", start, end, 15, late).phase == "closed"
    saturday = datetime(2026, 11, 7, 14, 0, tzinfo=timezone.utc)
    assert window_state("Toronto", "America/Toronto", start, end, 15, saturday).phase == "weekend"
    last = datetime(2026, 11, 4, 15, 30, 4, tzinfo=timezone.utc)
    assert window_state("Toronto", "America/Toronto", start, end, 15, last).ticks_left == 1


# ---------------------------------------------------------- the guess brake

class BrakeDB:
    def __init__(self, lane, pct=0.0, sent=0, bounced=0):
        self.lane, self.pct, self.sent, self.bounced = lane, pct, sent, bounced

    def recent_bounce_pct(self, window=100, before_local_date=""):
        return self.pct

    def bounces_on(self, _day):
        return self.sent, [{}] * self.bounced


GUESSES = SimpleNamespace(guess_max_bounce_pct=6)


def test_guesses_go_while_the_lane_is_clean():
    assert guess_hold(GUESSES, BrakeDB(VOLUME_LANE, 2.0, 10, 1), TODAY) == ""


def test_guesses_wait_once_the_recent_rate_is_over_the_limit():
    assert "7.0% over 6%" in guess_hold(GUESSES, BrakeDB(VOLUME_LANE, 7.0), TODAY)


def test_two_bounces_today_over_the_limit_stop_guesses_for_the_day():
    why = guess_hold(GUESSES, BrakeDB(VOLUME_LANE, 0.0, 10, 2), TODAY)
    assert "for today" in why and "2 of 10" in why


def test_one_bounce_alone_never_stops_them():
    assert guess_hold(GUESSES, BrakeDB(VOLUME_LANE, 0.0, 3, 1), TODAY) == ""


def test_the_main_lane_has_no_guess_brake_to_apply():
    assert guess_hold(GUESSES, BrakeDB("main", 50.0, 10, 9), TODAY) == ""


# ----------------------------------------------- the volume lane's send path

def volume_bot(volume_db):
    obj = object.__new__(Pipeline)
    obj.db = volume_db
    obj.settings = SimpleNamespace(
        require_for_send=lambda: None, send_timezone="America/Toronto",
        send_window_start=dtime(0, 0), send_window_end=dtime(23, 59),
        per_recipient_timezone=False, send_ramp_enabled=False,
        daily_send_limit=10, dry_run=True, resume_path="none.pdf",
        resume_link="https://drive.google.com/file/d/abc/view",
        mirror_to_drafts=False, followup_enabled=False, guess_max_bounce_pct=6,
    )
    return obj


def to_lines(out):
    return [line[4:].strip() for line in out.splitlines() if line.startswith("To: ")]


def queue_pair(main_db):
    a = add(main_db, "a@open.test")
    g = add(main_db, "g@guess.test", CATCHALL, PROV_GUESS, VOLUME_LANE)
    main_db.queue_draft(a, "Hi", f"Hello\n{LINK}\n")
    main_db.queue_draft(g, "Hi", f"Hello\n{LINK}\n")


def test_volume_leaves_open_drafts_to_main_until_main_is_full(lanes, capsys):
    main_db, volume_db = lanes
    queue_pair(main_db)
    volume_bot(volume_db).send(limit=10, ignore_window=True)
    out = capsys.readouterr().out
    assert to_lines(out) == ["g@guess.test"]
    assert "send[volume]: 1 open draft(s) left to the main lane" in out


def test_volume_takes_the_overflow_once_main_is_full(lanes, capsys):
    main_db, volume_db = lanes
    queue_pair(main_db)
    main_db.record_cap(TODAY, 1)
    send(main_db, add(main_db, "m@elsewhere.test"), "m@elsewhere.test")
    volume_bot(volume_db).send(limit=10, ignore_window=True)
    assert to_lines(capsys.readouterr().out) == ["a@open.test", "g@guess.test"]


def test_held_guesses_stay_queued(lanes, monkeypatch, capsys):
    main_db, volume_db = lanes
    queue_pair(main_db)
    monkeypatch.setattr(pipeline_module, "guess_hold", lambda *a: "guesses held, test")
    assert volume_bot(volume_db).send(limit=10, ignore_window=True) == 0
    out = capsys.readouterr().out
    assert "1 guess draft(s) held (guesses held, test)" in out
    assert [d["email"] for d in volume_db.queued_drafts(limit=None)] == [
        "a@open.test", "g@guess.test"]


# ---------------------------------------------------- follow-up share of cap

class ShareDB:
    lane = "main"

    def __init__(self, due, first_left, done=0):
        self.due, self.first_left, self.done = due, first_left, done

    def sent_count_on(self, _d):
        return self.done

    def followup_count_on(self, _d):
        return self.done

    def due_for_followup(self, *_a, **_k):
        return self.due

    def can_send_to_again(self, _e):
        return True, ""

    def queued_drafts(self, limit=100):
        return self.first_left

    def recent_bounce_pct(self, window=100, before_local_date=""):
        return 0.0


def share_bot(tmp_path, due, first_left):
    path = tmp_path / "f.txt"
    path.write_text("Subject: Re: {original_subject}\n\nHi {first_name}\n", encoding="utf-8")
    obj = object.__new__(Pipeline)
    obj.db = ShareDB(due, first_left)
    obj.settings = SimpleNamespace(
        require_for_send=lambda: None, send_timezone="America/Toronto",
        send_window_start=dtime(0, 0), send_window_end=dtime(23, 59),
        per_recipient_timezone=False, send_ramp_enabled=False, daily_send_limit=9,
        dry_run=True, followup_enabled=True, followup_template_path=path,
        followup_after_days=6, followup_max_total_sends=2, followup_share_pct=33,
        resume_path=tmp_path / "none.pdf", resume_link="https://x.test/cv",
        from_name="J", from_email="j@x.test", reply_to="", unsubscribe_mailto="j@x.test")
    return obj


def due_rows(n):
    return [{"id": i, "email": f"p{i}@x{i}.test", "first_name": "Sam", "last_name": "",
             "first_subject": "s", "first_message_id": "<m>", "company_name": "Co",
             "company_location": "Toronto", "verify_status": VERIFIED, "provenance": ""}
            for i in range(n)]


class NoBackend:
    def __enter__(self):
        return self

    def __exit__(self, *_e):
        return False


def test_followups_get_their_share_while_first_emails_wait(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pipeline_module, "make_backend", lambda s: NoBackend())
    share_bot(tmp_path, due_rows(10), [{"company_location": "Toronto"}]).followup(
        limit=50, ignore_window=True)
    assert capsys.readouterr().out.count("FOLLOWUP (dry run)") == 3


def test_followups_may_use_the_rest_once_no_first_email_is_left(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pipeline_module, "make_backend", lambda s: NoBackend())
    share_bot(tmp_path, due_rows(10), []).followup(limit=50, ignore_window=True)
    assert capsys.readouterr().out.count("FOLLOWUP (dry run)") == 9


# ------------------------------------------------------- settings and alerts

@pytest.fixture
def clean_env(monkeypatch):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("FROM_", "SMTP_", "IMAP_", "DAILY_", "ALERT_"))}
    monkeypatch.setattr(os, "environ", env)
    return env


def write_env(path, **values):
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")


def test_a_lane_file_layers_over_the_main_settings(tmp_path, clean_env):
    write_env(tmp_path / ".env", FROM_EMAIL="main@x.test", SMTP_USER="main@x.test",
              SMTP_PASS="m", DAILY_SEND_LIMIT="50", FROM_NAME="John")
    write_env(tmp_path / ".env.volume", FROM_EMAIL="vol@x.test", SMTP_USER="vol@x.test",
              SMTP_PASS="v", IMAP_USER="vol@x.test", IMAP_PASS="v",
              DAILY_SEND_LIMIT="5", ALERT_TO="main@x.test")
    s = Settings.load(tmp_path / ".env", lane="volume")
    assert (s.lane, s.from_email, s.smtp_user, s.daily_send_limit) == (
        "volume", "vol@x.test", "vol@x.test", 5)
    assert s.from_name == "John" and s.operator_email == "main@x.test"


def test_a_lane_without_its_own_inbox_login_is_refused(tmp_path, clean_env):
    write_env(tmp_path / ".env", FROM_EMAIL="main@x.test")
    write_env(tmp_path / ".env.volume", FROM_EMAIL="vol@x.test", SMTP_USER="vol@x.test",
              SMTP_PASS="v")
    with pytest.raises(ConfigError, match="IMAP_USER, IMAP_PASS"):
        Settings.load(tmp_path / ".env", lane="volume")


def test_a_lane_with_no_file_is_refused(tmp_path, clean_env):
    write_env(tmp_path / ".env", FROM_EMAIL="main@x.test")
    with pytest.raises(ConfigError, match=r"\.env\.volume"):
        Settings.load(tmp_path / ".env", lane="volume")


def test_a_lane_may_only_run_sending_side_commands(capsys):
    assert main(["--lane", "volume", "enrich"]) == 2
    assert "main lane only" in capsys.readouterr().err


def test_a_lanes_alerts_go_to_the_operator_and_its_own_file(tmp_path, monkeypatch):
    sent = []

    class Capture:
        def __enter__(self):
            return self

        def __exit__(self, *_e):
            return False

        def send(self, message):
            sent.append(message)
            return SimpleNamespace(ok=True, error="")

    monkeypatch.setattr(alerts, "make_backend", lambda _s: Capture())
    settings = SimpleNamespace(lane="volume", alert_to="boss@x.test",
                               from_email="vol@x.test", from_name="J",
                               db_path=tmp_path / "o.db")
    assert alerts._email(settings, "s", "b") == (True, "")
    assert "boss@x.test" in sent[0]["To"] and "vol@x.test" not in sent[0]["To"]
    path = alerts._write_alert_file(settings, "x")
    assert path.name == "ALERT-volume.txt"
