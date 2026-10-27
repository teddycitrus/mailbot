"""Which drafts a tick actually picks, and what the follow-up path refuses.

Two failures live here. A defective draft used to be screened out *after* the
tick's budget had been taken, so two bad drafts at the head of the queue sent
nothing at all, on every tick, for as long as they sat there. And the follow-up
path skipped guards the first email has had for months.
"""

from __future__ import annotations

from datetime import time as dtime
from types import SimpleNamespace


from src import pipeline as pipeline_module
from src.main import Pipeline


def draft(email, subject="Hi Acme", body="Hello there", attachments=""):
    return {
        "id": abs(hash(email)) % 10000,
        "contact_id": 1,
        "email": email,
        "subject": subject,
        "body": body,
        "attachments": attachments,
        "company_name": "Acme",
        "company_location": "Toronto",
        "first_name": "Sam",
        "last_name": "Rees",
    }


class PoolDB:
    def __init__(self, pool):
        self.pool = pool

    def queued_drafts(self, limit=100):
        return self.pool[:limit]

    def sent_count_on(self, _date):
        return 0

    def active_send_days(self, _date):
        return 0

    def recent_bounce_pct(self, window=100):
        return 0.0

    def due_for_followup(self, after_days, max_total_sends, limit=50):
        return []

    sent_companies: set = set()
    closed_companies: dict = {}

    def companies_sent_on(self, _date):
        return self.sent_companies

    def company_closed(self, company_id):
        return self.closed_companies.get(company_id, "")

    def mark_draft(self, draft_id, status):
        pass


def bare(pool, **over):
    """A Pipeline wired only as far as send()'s selection logic reaches."""
    obj = object.__new__(Pipeline)
    obj.db = PoolDB(pool)
    fields = dict(
        require_for_send=lambda: None,
        send_timezone="America/Toronto",
        send_window_start=dtime(0, 0),
        send_window_end=dtime(23, 59),
        per_recipient_timezone=False,
        send_ramp_enabled=False,
        daily_send_limit=25,
        dry_run=True,
        resume_path="assets/resume.pdf",
        resume_link="https://drive.google.com/file/d/abc/view",
        mirror_to_drafts=False,
    )
    fields.update(over)
    obj.settings = SimpleNamespace(**fields)
    return obj


# A body carrying the hosted resume link counts as complete; one with an
# unedited ALL-CAPS hole never does.
LINKED = "Hello there\n[my resume](https://drive.google.com/file/d/abc/view)"
HOLE = "I work on [YOUR FOCUS AREA]\n"


def sent_in_dry_run(out: str) -> list[str]:
    return [line.removeprefix("To: ").strip()
            for line in out.splitlines() if line.startswith("To: ")]


def test_a_defective_draft_does_not_consume_a_send_slot(capsys):
    """The stall. Two bad drafts at the head used to send nothing, every tick.

    Nothing marks a blocked draft, and the queue is ordered, so the same pair
    sat at the front of every subsequent tick. One unfinished template could
    stop the bot mailing anyone, indefinitely, while reporting that it had
    checked.
    """
    pool = [
        draft("bad1@acme.ai", body=HOLE),
        draft("bad2@acme.ai", body=HOLE),
        draft("good1@acme.ai", body=LINKED),
        draft("good2@acme.ai", body=LINKED),
        draft("good3@acme.ai", body=LINKED),
    ]
    bare(pool).send(limit=2, ignore_window=True)
    out = capsys.readouterr().out
    assert sent_in_dry_run(out) == ["good1@acme.ai", "good2@acme.ai"]
    assert "2 draft(s) blocked as incomplete" in out


def test_the_budget_is_still_respected_once_the_bad_ones_are_gone(capsys):
    pool = [draft(f"ok{i}@acme.ai", body=LINKED) for i in range(5)]
    bare(pool).send(limit=2, ignore_window=True)
    assert len(sent_in_dry_run(capsys.readouterr().out)) == 2


def test_a_queue_of_nothing_but_defects_still_refuses(capsys):
    pool = [draft("bad1@acme.ai", body=HOLE), draft("bad2@acme.ai", body=HOLE)]
    assert bare(pool).send(limit=2, ignore_window=True) == 0
    assert "no complete drafts to send" in capsys.readouterr().out


def test_the_daily_cap_still_beats_the_per_tick_limit(capsys):
    pool = [draft(f"ok{i}@acme.ai", body=LINKED) for i in range(10)]
    bot = bare(pool, send_ramp_enabled=False, daily_send_limit=3)
    bot.send(limit=25, ignore_window=True)
    assert len(sent_in_dry_run(capsys.readouterr().out)) == 3


def test_colleagues_are_not_mailed_on_the_same_day(capsys):
    pool = [draft("a@acme.ai", body=LINKED), draft("b@acme.ai", body=LINKED),
            draft("c@beta.ai", body=LINKED)]
    pool[0]["company_id"] = pool[1]["company_id"] = 1
    pool[2]["company_id"] = 2
    bare(pool).send(limit=5, ignore_window=True)
    assert sent_in_dry_run(capsys.readouterr().out) == ["a@acme.ai", "c@beta.ai"]


def test_a_company_mailed_earlier_today_waits_until_tomorrow(capsys):
    pool = [draft("b@acme.ai", body=LINKED), draft("c@beta.ai", body=LINKED)]
    pool[0]["company_id"], pool[1]["company_id"] = 1, 2
    bot = bare(pool)
    bot.db.sent_companies = {1}
    bot.send(limit=5, ignore_window=True)
    assert sent_in_dry_run(capsys.readouterr().out) == ["c@beta.ai"]


def test_a_company_that_replied_gets_nobody_else(capsys):
    pool = [draft("b@acme.ai", body=LINKED)]
    pool[0]["company_id"] = 1
    bot = bare(pool)
    bot.db.closed_companies = {1: "someone there already replied"}
    assert bot.send(limit=5, ignore_window=True) == 0
    assert sent_in_dry_run(capsys.readouterr().out) == []


# ------------------------------------------------------- the follow-up gates

def followup_bot(tmp_path, template_text, **over):
    path = tmp_path / "followup.txt"
    path.write_text(template_text, encoding="utf-8")
    obj = object.__new__(Pipeline)
    obj.db = PoolDB([])
    fields = dict(
        followup_enabled=True,
        require_for_send=lambda: None,
        followup_template_path=path,
        send_timezone="America/Toronto",
        send_window_start=dtime(0, 0),
        send_window_end=dtime(23, 59),
        per_recipient_timezone=False,
        send_ramp_enabled=False,
        daily_send_limit=25,
        followup_after_days=6,
        followup_max_total_sends=2,
        dry_run=True,
    )
    fields.update(over)
    obj.settings = SimpleNamespace(**fields)
    return obj


FINISHED = "Subject: Re: {original_subject}\n\nHi {first_name}, bumping this.\n"
UNFINISHED = "Subject: Re: {original_subject}\n\nHi {first_name}, [SAY SOMETHING]\n"


def test_an_unfinished_followup_template_is_refused(tmp_path, capsys):
    """send() blocks unedited holes in queued drafts. Nothing blocked them here.

    A follow-up is rendered at send time rather than queued, so it never met
    the draft guard, and the template checker only ever looked at the first
    contact template. That left exactly one route by which [BRACKETS] could
    reach a real person.
    """
    assert followup_bot(tmp_path, UNFINISHED).followup(limit=1) == 0
    out = capsys.readouterr().out
    assert "unedited placeholder" in out and "[SAY SOMETHING]" in out


def test_a_followup_outside_the_window_is_refused(tmp_path, monkeypatch, capsys):
    """Without per-recipient windows there is one clock, and it applies here too.

    send() has always refused against it. followup() did not look at it at all,
    so a nudge was the one thing this bot could deliver at 3am.
    """
    monkeypatch.setattr(pipeline_module, "in_send_window",
                        lambda *a, **k: (False, "03:00 America/Toronto outside window"))
    assert followup_bot(tmp_path, FINISHED).followup(limit=1) == 0
    assert "followup: refused" in capsys.readouterr().out


def test_ignore_window_still_overrides_the_clock(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pipeline_module, "in_send_window",
                        lambda *a, **k: (False, "outside window"))
    bot = followup_bot(tmp_path, FINISHED)
    # Gets past the clock, then stops on the cap lookup rather than the window.
    bot.followup(limit=1, ignore_window=True)
    assert "followup: refused" not in capsys.readouterr().out


def test_per_recipient_mode_leaves_the_gate_to_the_per_draft_check(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pipeline_module, "in_send_window",
                        lambda *a, **k: (False, "outside window"))
    bot = followup_bot(tmp_path, FINISHED, per_recipient_timezone=True)
    bot.followup(limit=1)
    assert "followup: refused" not in capsys.readouterr().out
