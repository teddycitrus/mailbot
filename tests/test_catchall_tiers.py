"""Catch-all addresses get stricter as the bounce rate climbs.

A catch-all domain accepts any recipient, so the SMTP probe proves nothing
and provenance is the only evidence. When mail starts bouncing, the weaker
evidence goes first: inferred addresses (house pattern, commit history),
then catch-alls altogether. Verified mailboxes are never affected. All
addresses here are synthetic.
"""

from __future__ import annotations

from datetime import time as dtime
from types import SimpleNamespace

from src import pipeline as pipeline_module
from src.config import Settings
from src.main import Pipeline
from src.models import CATCHALL, SELF_PUBLISHED, TRUSTED_PROVENANCE, VERIFIED


class _Stub:
    catchall_provenance = Settings.catchall_provenance

    def __init__(self, **fields):
        self.__dict__.update(fields)


def tiers(limit=3):
    return _Stub(catchall_strict_bounce_pct=limit)


def test_normal_bounce_rate_keeps_every_trusted_source():
    allowed, why = tiers().catchall_provenance(1.0)
    assert allowed == TRUSTED_PROVENANCE and "trusted" in why


def test_over_the_limit_only_self_published_catchalls_go():
    allowed, why = tiers().catchall_provenance(3.5)
    assert allowed == SELF_PUBLISHED and "3.5%" in why


def test_over_twice_the_limit_no_catchall_goes():
    allowed, why = tiers().catchall_provenance(6.5)
    assert allowed == frozenset() and "off" in why


def test_exactly_at_the_limit_is_still_normal():
    assert tiers().catchall_provenance(3.0)[0] == TRUSTED_PROVENANCE


def test_zero_turns_the_tiers_off():
    assert tiers(0).catchall_provenance(50.0)[0] == TRUSTED_PROVENANCE


# --------------------------------------------------------------- send path

LINK = "[my resume](https://drive.google.com/file/d/abc/view)"


def draft(email, verify, provenance, company_id):
    return {"id": company_id, "contact_id": company_id, "email": email,
            "subject": "Hi", "body": f"Hello\n{LINK}\n", "attachments": "",
            "company_name": f"Co{company_id}", "company_location": "Toronto",
            "company_id": company_id, "first_name": "Ana", "last_name": "Li",
            "verify_status": verify, "provenance": provenance}


class DB:
    def __init__(self, pool, pct):
        self.pool, self.pct = pool, pct

    def queued_drafts(self, limit=100):
        return self.pool if limit is None else self.pool[:limit]

    def sent_count_on(self, _d):
        return 0

    def recent_bounce_pct(self, window=100, before_local_date=""):
        return self.pct

    def companies_sent_on(self, _d):
        return set()

    def company_closed(self, _c, _provenance=""):
        return ""

    def mark_draft(self, *_a):
        raise AssertionError("a held draft must stay queued")


def bot(pool, pct):
    obj = object.__new__(Pipeline)
    obj.db = DB(pool, pct)
    obj.settings = SimpleNamespace(
        require_for_send=lambda: None, send_timezone="America/Toronto",
        send_window_start=dtime(0, 0), send_window_end=dtime(23, 59),
        per_recipient_timezone=False, send_ramp_enabled=False,
        daily_send_limit=25, dry_run=True, resume_path="none.pdf",
        resume_link="https://drive.google.com/file/d/abc/view",
        mirror_to_drafts=False, catchall_strict_bounce_pct=3,
        catchall_provenance=lambda pct: Settings.catchall_provenance(
            SimpleNamespace(catchall_strict_bounce_pct=3), pct),
    )
    return obj


POOL = [
    draft("v@one.test", VERIFIED, "guess", 1),
    draft("p@two.test", CATCHALL, "founder_published", 2),
    draft("h@three.test", CATCHALL, "house_pattern", 3),
    draft("g@four.test", CATCHALL, "git_commit", 4),
]


def sent(out):
    return [l[4:].strip() for l in out.splitlines() if l.startswith("To: ")]


def test_send_at_a_normal_rate_mails_every_trusted_catchall(capsys):
    bot(POOL, 1.0).send(limit=10, ignore_window=True)
    assert sent(capsys.readouterr().out) == [
        "v@one.test", "p@two.test", "h@three.test", "g@four.test"]


def test_send_over_the_limit_holds_inferred_catchalls(capsys):
    bot(POOL, 4.0).send(limit=10, ignore_window=True)
    out = capsys.readouterr().out
    assert sent(out) == ["v@one.test", "p@two.test"]
    assert "2 catch-all draft(s) held" in out


def test_send_over_twice_the_limit_mails_verified_only(capsys):
    bot(POOL, 7.0).send(limit=10, ignore_window=True)
    assert sent(capsys.readouterr().out) == ["v@one.test"]


def test_followup_over_twice_the_limit_skips_catchalls(tmp_path, monkeypatch, capsys):
    path = tmp_path / "f.txt"
    path.write_text("Subject: Re: {original_subject}\n\nHi {first_name}\n", encoding="utf-8")

    class FDB(DB):
        def due_for_followup(self, *_a, **_k):
            return [dict(r, id=r["contact_id"], first_subject="s", first_message_id="<m>")
                    for r in POOL]

        def can_send_to_again(self, _e):
            return True, ""

    class NoBackend:
        def __enter__(self):
            return self

        def __exit__(self, *_e):
            return False

    obj = bot(POOL, 7.0)
    obj.db = FDB(POOL, 7.0)
    obj.settings.__dict__.update(
        followup_enabled=True, followup_template_path=path,
        followup_after_days=6, followup_max_total_sends=2,
        from_name="J", from_email="j@x.test", reply_to="", unsubscribe_mailto="j@x.test")
    monkeypatch.setattr(pipeline_module, "make_backend", lambda s: NoBackend())
    obj.followup(limit=10, ignore_window=True)
    out = capsys.readouterr().out
    assert out.count("FOLLOWUP (dry run)") == 1 and "to v@one.test" in out
    assert "3 catch-all off" in out
