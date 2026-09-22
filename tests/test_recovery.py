"""The verification outage, its blast radius, and the repair.

The prober stopped answering partway through nearly every long run, and every
verdict it produced after that recorded the failed probe rather than a mailbox.
Nothing raised. These pin the three halves: the latch that caused it, the cache
it poisoned on the way past, and the recovery that puts both right.
"""

from __future__ import annotations

import pytest

from src.database import Database
from src.doctor import FAIL, OK, WARN, _verification
from src.locking import Busy, single_run
from src.models import (
    CATCHALL, Company, Contact, NO_CONTACTS, SRC_SCRAPED, VERIFIED,
)
from src.reverify import recover, reverify_contacts
from src.verifier import Verifier


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


# --------------------------------------------------- the latch that started it

class ScriptedSMTP:
    """A server that connects or refuses according to a list handed to it."""

    script: list = []

    def __init__(self, host, port, timeout=None):
        if not ScriptedSMTP.script.pop(0):
            raise OSError("connection refused")

    def ehlo(self, *_a):
        return (250, b"ok")

    def mail(self, *_a):
        return (250, b"ok")

    def rcpt(self, _addr):
        return (250, b"OK")

    def quit(self):
        return None


@pytest.fixture
def scripted(monkeypatch):
    def run(outcomes):
        ScriptedSMTP.script = list(outcomes)
        monkeypatch.setattr("src.verifier.smtplib.SMTP", ScriptedSMTP)
        return Verifier(db=None)
    return run


def test_a_successful_probe_clears_the_failure_streak(scripted):
    """Four dead hosts then a live one must leave no debt behind."""
    v = scripted([False] * 4 + [True])
    for _ in range(4):
        v._rcpt("mx.acme.ai", ["a@acme.ai"])
    assert v.timeouts == 4 and not v.throttled
    v._rcpt("mx.acme.ai", ["b@acme.ai"])
    assert v.timeouts == 0, "a completed handshake proves we are not throttled"
    assert not v.throttled


def test_scattered_failures_never_add_up_to_a_pause(scripted):
    """The exact shape of the bug: a long run meets dead hosts and carries on.

    Ninety companies with a dead host every other one is ordinary. Counting
    those against a single lifetime budget turned verification off for the rest
    of the run, and every later contact was filed as "no SMTP answer".
    """
    v = scripted([False, True] * 12)
    for _ in range(24):
        v._rcpt("mx.acme.ai", ["a@acme.ai"])
    assert not v.throttled, "isolated failures must not latch the prober off"


def test_an_unbroken_streak_still_pauses_probing(scripted):
    """The brake itself must survive: real throttling should still stop us."""
    v = scripted([False] * Verifier.TIMEOUT_BUDGET)
    for _ in range(Verifier.TIMEOUT_BUDGET):
        v._rcpt("mx.acme.ai", ["a@acme.ai"])
    assert v.throttled


# ------------------------------------------------- the cache it poisoned

class RecordingDB:
    """Just enough database to see what the verifier writes to the cache."""

    def __init__(self):
        self.writes: list[tuple] = []

    def cached_domain(self, _domain):
        return None

    def cache_domain(self, domain, mx_host, catchall):
        self.writes.append((domain, mx_host, catchall))


class SilentProbe(Verifier):
    def mx_host(self, _domain):
        return "mx.acme.ai"

    def _rcpt(self, _host, addresses):
        return {a: (0, b"timed out") for a in addresses}


class AnsweringProbe(Verifier):
    """Answers every recipient with one code. A blanket 250 is a catch-all."""

    def __init__(self, code, **kw):
        super().__init__(**kw)
        self.code = code

    def mx_host(self, _domain):
        return "mx.acme.ai"

    def _rcpt(self, _host, addresses):
        return {a: (self.code, b"answer") for a in addresses}


class SelectiveProbe(Verifier):
    """Accepts the mailboxes it knows and rejects the rest, as a real one does.

    The distinction matters: the catch-all test works by probing a random
    address, so a server that says 250 to everything is a catch-all and no
    address on it can be confirmed.
    """

    def __init__(self, accepts, **kw):
        super().__init__(**kw)
        self.accepts = set(accepts)

    def mx_host(self, _domain):
        return "mx.acme.ai"

    def _rcpt(self, _host, addresses):
        return {a: ((250, b"OK") if a in self.accepts else (550, b"No such user"))
                for a in addresses}


def test_an_unanswered_probe_records_nothing():
    """Silence is not evidence of anything, least of all "not a catch-all".

    This is what made the outage outlive itself: mx_host reloads the cache on
    every later run, so a guess written down during the outage was still being
    trusted long after probing recovered.
    """
    store = RecordingDB()
    v = SilentProbe(db=store)
    assert v.is_catchall("acme.ai") is False
    assert store.writes == [], "must not cache a verdict it never received"
    assert "acme.ai" not in v._catchall, "and must not remember it in-process"


def test_a_real_catchall_answer_is_still_cached():
    store = RecordingDB()
    assert AnsweringProbe(250, db=store).is_catchall("acme.ai") is True
    assert store.writes == [("acme.ai", "mx.acme.ai", True)]


def test_a_real_rejection_is_still_cached():
    store = RecordingDB()
    assert AnsweringProbe(550, db=store).is_catchall("acme.ai") is False
    assert store.writes == [("acme.ai", "mx.acme.ai", False)]


# ------------------------------------------------------------ the repair

SILENT = "no SMTP answer (0); trusting published source"


def _pending(db, email, detail, confidence, company_id=None):
    return db.upsert_contact(Contact(
        email=email, company_id=company_id, source=SRC_SCRAPED,
        verify_status="unverified", verify_detail=detail, confidence=confidence,
    ))


def test_only_failed_probes_are_picked_up_for_repair(db):
    _pending(db, "silent@acme.ai", SILENT, 70)
    _pending(db, "vague@acme.ai", "inconclusive 0", 40)
    _pending(db, "paused@acme.ai", "probing paused, server throttling", 55)
    _pending(db, "proven@acme.ai", "mailbox accepted", 95)
    _pending(db, "known@acme.ai", "catch-all domain, address was published", 75)

    found = {row["email"] for row in db.contacts_needing_reverify()}
    assert found == {"silent@acme.ai", "vague@acme.ai", "paused@acme.ai"}, (
        "a real verdict must never be thrown away and re-probed")


def test_a_verdict_behind_a_provenance_prefix_is_still_found(db):
    """The HN importer writes its own provenance in front of the verdict.

    That makes the text "published on HN: no SMTP answer (0); ...". A pattern
    anchored at the start of the string matched none of them, so every
    HN-sourced contact kept its outage-era verdict through the repair.
    """
    _pending(db, "poster@acme.ai", "published on HN: " + SILENT, 75)
    found = {row["email"] for row in db.contacts_needing_reverify()}
    assert found == {"poster@acme.ai"}


def test_a_contact_with_a_draft_already_waiting_is_repaired_first(db):
    """Queued means a draft exists and the next tick will send it.

    Restricting the repair to pending contacts left every queued draft
    carrying an outage-era verdict, and two of them bounced.
    """
    company = db.upsert_company(Company(name="Acme", domain="acme.ai"))
    waiting = _pending(db, "waiting@acme.ai", SILENT, 70, company)
    _pending(db, "later@acme.ai", SILENT, 95, company)
    db.queue_draft(waiting, "Hi", "Hello", ["resume.pdf"])

    order = [row["email"] for row in db.contacts_needing_reverify()]
    assert order[0] == "waiting@acme.ai", "a draft on the wire comes first"
    assert set(order) == {"waiting@acme.ai", "later@acme.ai"}


def test_a_dead_address_takes_its_queued_draft_down_with_it(db):
    """Suppression stops the scheduled send. The draft has to go too.

    A copy of every queued draft sits in Gmail Drafts so it can be sent by
    hand from a phone. Suppressing the contact does not touch that copy; only
    marking the draft does, which is what lets the mirror clean it up.
    """
    company = db.upsert_company(Company(name="Acme", domain="acme.ai"))
    contact = _pending(db, "gone@acme.ai", SILENT, 70, company)
    draft_id = db.queue_draft(contact, "Hi", "Hello", ["resume.pdf"])
    db.set_draft_mirror(draft_id, "<copy@gmail>")
    assert len(db.queued_drafts()) == 1

    report = reverify_contacts(db, AnsweringProbe(550, db=db))

    assert report.drafts_pulled == 1
    assert db.queued_drafts() == [], "nothing left for a tick to send"
    assert [r["email"] for r in db.stale_mirrors()] == ["gone@acme.ai"], (
        "the Gmail copy must be queued for deletion, not left on the phone")


def test_repair_lifts_confidence_on_a_mailbox_that_answers(db):
    _pending(db, "sara@acme.ai", SILENT, 70)
    report = reverify_contacts(db, SelectiveProbe({"sara@acme.ai"}, db=db))
    assert report.examined == 1
    assert [e for e, _was, _now, _why in report.improved] == ["sara@acme.ai"]
    row = db.find_contact_by_email("sara@acme.ai")
    assert row["verify_status"] == VERIFIED and row["confidence"] >= 90


def test_repair_on_a_catchall_domain_settles_for_what_it_can_prove(db):
    """A domain that accepts everything cannot confirm anyone, and says so.

    Still an improvement on the outage verdict: the address was published, so
    it stays sendable, but it is labelled for what it is rather than carrying
    a number that implies a probe succeeded.
    """
    _pending(db, "sara@acme.ai", SILENT, 70)
    reverify_contacts(db, AnsweringProbe(250, db=db))
    row = db.find_contact_by_email("sara@acme.ai")
    assert row["verify_status"] == CATCHALL
    assert "catch-all" in row["verify_detail"]


def test_repair_suppresses_an_address_the_server_rejects(db):
    _pending(db, "gone@acme.ai", SILENT, 70)
    report = reverify_contacts(db, AnsweringProbe(550, db=db))
    assert [e for e, _why in report.dropped] == ["gone@acme.ai"]
    assert db.is_suppressed("gone@acme.ai")
    allowed, why = db.can_send_to("gone@acme.ai")
    assert not allowed and why == "suppressed"


def test_repair_never_touches_someone_already_written_to(db):
    """The one rule the whole project rests on. Repair is not a second chance."""
    company = db.upsert_company(Company(name="Acme", domain="acme.ai"))
    contact = _pending(db, "sent@acme.ai", SILENT, 70, company)
    db.record_send(contact, "sent@acme.ai", "Hi", "<id@acme>", "smtp",
                   "2026-09-18")

    assert db.contacts_needing_reverify() == []
    reverify_contacts(db, AnsweringProbe(250, db=db))
    allowed, why = db.can_send_to("sent@acme.ai")
    assert not allowed
    assert why in {"already sent", "already in send log"}


def test_repair_stops_rather_than_rewriting_one_bad_verdict_as_another(db):
    for i in range(4):
        _pending(db, f"a{i}@acme.ai", SILENT, 70)
    v = SilentProbe(db=db)
    v.throttled = True
    report = reverify_contacts(db, v)
    assert report.examined == 0, "a throttled prober has nothing to contribute"


def test_parked_companies_go_back_in_the_queue(db):
    for name, reason in (("A", "no candidate address accepted"),
                         ("B", "catch-all domain, guesses unprovable"),
                         ("C", "domain has no MX record, cannot receive mail"),
                         ("D", "no founders listed and no published address")):
        cid = db.upsert_company(Company(name=name, domain=f"{name.lower()}.ai"))
        db.set_company_status(cid, NO_CONTACTS, reason)

    assert db.reopen_for_enrich() == 2
    reopened = {r["name"] for r in db.companies_by_status("new")}
    assert reopened == {"A", "B"}, (
        "a domain with no MX is a finding, not a failed probe, and stays parked")


def test_recover_clears_the_poisoned_cache_first(db):
    db.cache_domain("guessed.ai", "mx.guessed.ai", False)
    db.cache_domain("proven.ai", "mx.proven.ai", True)
    report = recover(db, AnsweringProbe(250, db=db), reopen=False)
    assert report.cache_cleared == 1
    assert db.cached_domain("guessed.ai") is None
    assert db.cached_domain("proven.ai") is not None, (
        "a catch-all verdict needed a real 250, so it is still evidence")


# ------------------------------------------- noticing it next time

def test_health_check_spots_a_pool_built_from_failed_probes(db):
    for i in range(30):
        _pending(db, f"a{i}@acme.ai", SILENT, 70)
    check = _verification(db)
    assert check.state == FAIL
    assert "reverify" in check.detail


def test_health_check_is_happy_when_probes_are_answering(db):
    for i in range(30):
        _pending(db, f"a{i}@acme.ai", "mailbox accepted", 95)
    assert _verification(db).state == OK


def test_health_check_declines_to_judge_a_tiny_pool(db):
    _pending(db, "a@acme.ai", SILENT, 70)
    assert _verification(db).state == WARN


# --------------------------------------------------------- one run at a time

def test_a_second_run_stands_down(tmp_path):
    lock = tmp_path / "outreach.lock"
    with single_run(lock):
        with pytest.raises(Busy):
            with single_run(lock, wait_seconds=0.05):
                pass


def test_the_lock_is_released_when_the_run_ends(tmp_path):
    lock = tmp_path / "outreach.lock"
    with single_run(lock):
        pass
    with single_run(lock, wait_seconds=0.05):
        pass


def test_the_lock_is_released_even_when_the_run_raises(tmp_path):
    lock = tmp_path / "outreach.lock"
    with pytest.raises(ValueError):
        with single_run(lock):
            raise ValueError("boom")
    with single_run(lock, wait_seconds=0.05):
        pass
