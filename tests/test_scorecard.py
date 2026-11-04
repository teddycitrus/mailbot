"""The offline scorecard: reads a snapshot, never writes it, and reports
label quality, segment rates, gate replay and ramp history faithfully.

All data here is synthetic. No real recipient addresses appear.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from src.database import Database
from src.models import Company, Contact
from src.scorecard import (
    gate_would_refuse, label_audit, open_ro, queue_supply, ramp_replay,
    recent_bounce_pct, run, segment_rates, send_table, wilson,
)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "snap.db")
    yield database
    database.close()


def _contact(db, email, company="Example", domain="", verify="verified",
             provenance="founder_published", first_name="Ada", status=None):
    domain = domain or email.split("@")[1]
    cid = db.upsert_company(Company(name=company, domain=domain))
    contact_id = db.upsert_contact(Contact(
        email=email, company_id=cid, first_name=first_name,
        verify_status=verify, provenance=provenance))
    if status:
        db.conn.execute("UPDATE contacts SET status=? WHERE id=?", (status, contact_id))
        db.conn.commit()
    return contact_id


def _send(db, contact_id, email, day, at="12:00:00"):
    db.conn.execute(
        """INSERT INTO sends (contact_id, email, subject, message_id, backend,
           status, error, sent_at, local_date) VALUES (?,?,?,?,?,'sent','',?,?)""",
        (contact_id, email, "s", "mid", "smtp", f"{day}T{at}+00:00", day))
    db.conn.commit()


def _event(db, kind, ref, day, at="13:00:00"):
    db.conn.execute(
        "INSERT INTO events (kind, ref, detail, created_at) VALUES (?,?,'',?)",
        (kind, ref, f"{day}T{at}+00:00"))
    db.conn.commit()


# ------------------------------------------------------------------- wilson

def test_wilson_matches_known_values():
    lo, hi = wilson(9, 100)
    assert 4.0 < lo < 5.0
    assert 16.0 < hi < 17.0


def test_wilson_handles_zero_total():
    assert wilson(0, 0) == (0.0, 0.0)


# ------------------------------------------------------------ read-only open

def test_open_ro_refuses_writes(db, tmp_path):
    _contact(db, "ada@example-a.test")
    conn = open_ro(tmp_path / "snap.db")
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO events (kind, ref, detail, created_at) "
                     "VALUES ('x','','','2026-01-01')")
    conn.close()


# ------------------------------------------------------------- gate replay

def test_gate_refuses_role_local():
    assert gate_would_refuse("hello@example-a.test", "verified", "") != ""


def test_gate_refuses_unattributed_catchall():
    why = gate_would_refuse("ada@example-a.test", "catchall", "site_unattributed")
    assert "not deliverable" in why


def test_gate_passes_verified_and_trusted_catchall():
    assert gate_would_refuse("ada@example-a.test", "verified", "guess") == ""
    assert gate_would_refuse("ada@example-a.test", "catchall", "hn_post") == ""


# -------------------------------------------------------------- send table

def test_bounce_attributed_to_the_send_before_the_event(db, tmp_path):
    cid = _contact(db, "ada@example-a.test", status="bounced")
    _send(db, cid, "ada@example-a.test", "2026-09-01")
    _send(db, cid, "ada@example-a.test", "2026-09-08")
    _event(db, "bounce", "ada@example-a.test", "2026-09-08")
    conn = open_ro(tmp_path / "snap.db")
    sends = send_table(conn)
    conn.close()
    assert [s["bounced"] for s in sends] == [False, True]
    assert sends[1]["touch_no"] == 2
    assert not sends[1]["label_estimated"]


def test_label_without_event_is_flagged_estimated(db, tmp_path):
    cid = _contact(db, "bob@example-b.test", status="replied")
    _send(db, cid, "bob@example-b.test", "2026-09-01")
    conn = open_ro(tmp_path / "snap.db")
    sends = send_table(conn)
    conn.close()
    assert sends[0]["replied"] and sends[0]["label_estimated"]


def test_an_unsubscribe_logged_as_a_reply_is_not_a_reply(db, tmp_path):
    cid = _contact(db, "cy@example-c.test", status="replied")
    _send(db, cid, "cy@example-c.test", "2026-09-01")
    db.conn.execute(
        "INSERT INTO events (kind, ref, detail, created_at) "
        "VALUES ('reply', 'cy@example-c.test', 'unsubscribe', '2026-09-02T09:00:00+00:00')")
    db.conn.commit()
    conn = open_ro(tmp_path / "snap.db")
    sends, audit = send_table(conn), label_audit(conn)
    conn.close()
    assert not sends[0]["replied"]
    assert audit["refusals_logged_as_replies"] == 1


# ------------------------------------------------------------- label audit

def test_label_audit_counts_event_backing(db, tmp_path):
    cid = _contact(db, "ada@example-a.test", status="bounced")
    _send(db, cid, "ada@example-a.test", "2026-09-01")
    _event(db, "bounce", "ada@example-a.test", "2026-09-01")
    _event(db, "bounce", "ada@example-a.test", "2026-09-02")
    _contact(db, "bob@example-b.test", company="B", status="replied")
    conn = open_ro(tmp_path / "snap.db")
    audit = label_audit(conn)
    conn.close()
    assert audit["bounced_contacts"] == 1
    assert audit["bounce_refs_with_repeats"] == 1
    assert audit["bounced_without_event"] == 0
    assert audit["replied_without_event"] == 1


# -------------------------------------------------------------- ramp replay

def test_recent_bounce_pct_matches_database_method(db, tmp_path):
    """The replay's recurrable-only series is the live ramp input; the raw
    series exists so the report can show what was discounted."""
    for i in range(10):
        email = f"p{i}@example-{i}.test"
        status = "bounced" if i < 2 else "sent"
        verify = "verified" if i == 0 else "catchall"
        prov = "guess" if i == 0 else "site_unattributed"
        cid = _contact(db, email, company=f"C{i}", verify=verify,
                       provenance=prov, status=None)
        _send(db, cid, email, "2026-09-01")
        db.conn.execute("UPDATE contacts SET status=? WHERE id=?", (status, cid))
    db.conn.commit()
    live = db.recent_bounce_pct(window=100, before_local_date="2026-09-02")
    conn = open_ro(tmp_path / "snap.db")
    raw = recent_bounce_pct(conn, "2026-09-02")
    recurrable = recent_bounce_pct(conn, "2026-09-02", recurrable_only=True)
    conn.close()
    assert raw == 20.0
    assert recurrable == live == 10.0


def test_recurrable_only_discounts_gate_refused_bounces(db, tmp_path):
    cid1 = _contact(db, "ada@example-a.test", verify="catchall",
                    provenance="site_unattributed")
    _send(db, cid1, "ada@example-a.test", "2026-09-01")
    cid2 = _contact(db, "bea@example-b.test", company="B", verify="verified",
                    provenance="guess")
    _send(db, cid2, "bea@example-b.test", "2026-09-01")
    for cid in (cid1, cid2):
        db.conn.execute("UPDATE contacts SET status='bounced' WHERE id=?", (cid,))
    db.conn.commit()
    conn = open_ro(tmp_path / "snap.db")
    assert recent_bounce_pct(conn, "2026-09-02") == 100.0
    # ada's class is refused by today's gate, so only bea's bounce can recur.
    assert recent_bounce_pct(conn, "2026-09-02", recurrable_only=True) == 50.0
    conn.close()


def test_ramp_replay_holds_cap_over_threshold(db, tmp_path):
    for i in range(10):
        email = f"p{i}@example-{i}.test"
        cid = _contact(db, email, company=f"C{i}")
        _send(db, cid, email, "2026-09-01")
        if i < 1:
            db.conn.execute("UPDATE contacts SET status='bounced' WHERE id=?", (cid,))
    db.conn.commit()
    for i in range(3):
        email = f"q{i}@example-q{i}.test"
        cid = _contact(db, email, company=f"D{i}")
        _send(db, cid, email, "2026-09-02")
    conn = open_ro(tmp_path / "snap.db")
    series = ramp_replay(conn)
    conn.close()
    day2 = series[1]
    assert day2["date"] == "2026-09-02"
    assert day2["bounce_pct"] == 10.0
    assert day2["held"] and day2["cap"] == 8


def test_ramp_replay_held_follows_the_recurrable_series(db, tmp_path):
    """The live ramp discounts bounces the gates now refuse, so the replay's
    held flag must key on the recurrable rate, not the raw one."""
    for i in range(10):
        email = f"p{i}@example-{i}.test"
        verify = "catchall" if i < 1 else "verified"
        prov = "site_unattributed" if i < 1 else "guess"
        cid = _contact(db, email, company=f"C{i}", verify=verify, provenance=prov)
        _send(db, cid, email, "2026-09-01")
        if i < 1:
            db.conn.execute("UPDATE contacts SET status='bounced' WHERE id=?", (cid,))
    db.conn.commit()
    for i in range(3):
        email = f"q{i}@example-q{i}.test"
        cid = _contact(db, email, company=f"D{i}")
        _send(db, cid, email, "2026-09-02")
    conn = open_ro(tmp_path / "snap.db")
    series = ramp_replay(conn)
    conn.close()
    day2 = series[1]
    assert day2["bounce_pct"] == 10.0
    assert day2["bounce_pct_recurrable"] == 0.0
    assert not day2["held"] and day2["cap"] == 10


# ------------------------------------------------------------- queue supply

def test_queue_supply_applies_the_gate(db, tmp_path):
    _contact(db, "ada@example-a.test", verify="verified", provenance="guess")
    _contact(db, "bea@example-b.test", company="B", verify="catchall",
             provenance="site_unattributed")
    cid = _contact(db, "cat@example-c.test", company="C", verify="catchall",
                   provenance="site_unattributed")
    db.queue_draft(cid, "s", "b")
    conn = open_ro(tmp_path / "snap.db")
    supply = queue_supply(conn)
    conn.close()
    assert supply["eligible_now"]["pending"] == 1
    assert sum(supply["pending_or_queued_refused"].values()) == 2
    assert supply["queued_drafts_gate_refused"] == 1


# -------------------------------------------------------------------- run()

def test_run_writes_reports_and_leaves_the_db_untouched(db, tmp_path):
    cid = _contact(db, "ada@example-a.test", status="bounced")
    _send(db, cid, "ada@example-a.test", "2026-09-01")
    _event(db, "bounce", "ada@example-a.test", "2026-09-01")
    db.close()
    db_file = tmp_path / "snap.db"
    before = db_file.read_bytes()
    out = tmp_path / "out"
    result = run(db_file, out, split="2026-10-01")
    assert (out / "scorecard.json").exists()
    assert (out / "scorecard.md").exists()
    assert db_file.read_bytes() == before
    assert result["train_sends"] == 1
    assert not (out / "holdout_access.log").exists()
    parsed = json.loads((out / "scorecard.json").read_text(encoding="utf-8"))
    assert parsed["label_audit"]["bounced_contacts"] == 1


def test_holdout_access_is_logged(db, tmp_path):
    cid = _contact(db, "ada@example-a.test")
    _send(db, cid, "ada@example-a.test", "2026-10-25")
    db.close()
    out = tmp_path / "out"
    run(tmp_path / "snap.db", out, holdout=True)
    assert "holdout read" in (out / "holdout_access.log").read_text(encoding="utf-8")
