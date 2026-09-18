"""Verification rules, with the SMTP layer mocked out."""

from __future__ import annotations

import pytest

from src.models import (
    CATCHALL, NO_MX, SRC_INFERRED, SRC_SCRAPED, UNDELIVERABLE, UNVERIFIED,
    VERIFIED,
)
from src.verifier import (
    Verifier, candidate_addresses, first_name_from_email, is_never_send,
    is_role_account, valid_syntax,
)


@pytest.mark.parametrize("email,ok", [
    ("stefan@acme.ai", True),
    ("first.last@sub.acme.co.uk", True),
    ("no-at-sign.com", False),
    ("spaces in@acme.ai", False),
    ("trailing@acme", False),
])
def test_syntax(email, ok):
    assert valid_syntax(email) is ok


def test_role_and_never_send_classification():
    assert is_role_account("hello@acme.ai")
    assert is_role_account("careers@acme.ai")
    assert not is_role_account("stefan@acme.ai")
    assert is_never_send("postmaster@acme.ai")
    assert is_never_send("no-reply@acme.ai")
    assert not is_never_send("hello@acme.ai")


@pytest.mark.parametrize("email", [
    "noreply@acme.ai",
    "no_reply@acme.ai",
    "NoReply@acme.ai",
    "noreply-jobs@acme.ai",
    "jobs.no-reply@acme.ai",
    "do-not-reply@acme.ai",
    "bounces+7a1f@acme.ai",
    "mailer-daemon@acme.ai",
    "1234+bob@users.noreply.github.com",
    "ticket-88@reply.acme.ai",
    "campaign@mailer.acme.ai",
])
def test_addresses_that_nobody_reads_are_never_sent_to(email):
    assert is_never_send(email)


@pytest.mark.parametrize("email", [
    "stefan@acme.ai",
    "hello@acme.ai",
    "careers@acme.ai",
    # reply.io is a real company, so the host check stops at the registrable
    # domain and leaves its people reachable.
    "stefan@reply.io",
])
def test_real_mailboxes_survive_the_never_send_check(email):
    assert not is_never_send(email)


def test_candidate_patterns_are_ordered_and_unique():
    cands = candidate_addresses("Stefan", "Seltz-Axmacher", "acme.ai")
    assert cands[0] == "stefan@acme.ai"
    assert "stefan.seltzaxmacher@acme.ai" in cands
    assert len(cands) == len(set(cands))


def test_candidate_patterns_handle_single_name():
    assert candidate_addresses("Cher", "", "acme.ai") == ["cher@acme.ai"]
    assert candidate_addresses("", "", "acme.ai") == []


class FakeVerifier(Verifier):
    """Verifier with the network replaced by a scripted response table."""

    def __init__(self, mx="mx.acme.ai", accepts=(), catchall=False):
        super().__init__(db=None)
        self._fake_mx = mx
        self._accepts = set(accepts)
        self._fake_catchall = catchall

    def mx_host(self, domain):
        return self._fake_mx

    def _rcpt(self, host, addresses):
        out = {}
        for addr in addresses:
            if self._fake_catchall or addr in self._accepts:
                out[addr] = (250, b"OK")
            else:
                out[addr] = (550, b"No such user")
        return out


def test_verified_mailbox_scores_high():
    v = FakeVerifier(accepts={"stefan@acme.ai"})
    verdict = v.verify("stefan@acme.ai")
    assert verdict.status == VERIFIED
    assert verdict.confidence >= 90


def test_rejected_mailbox_is_undeliverable():
    v = FakeVerifier(accepts=set())
    verdict = v.verify("nobody@acme.ai")
    assert verdict.status == UNDELIVERABLE
    assert verdict.confidence == 0


def test_missing_mx_is_not_sendable():
    v = FakeVerifier(mx=None)
    verdict = v.verify("stefan@acme.ai")
    assert verdict.status == NO_MX
    assert not verdict.sendable


def test_guessed_address_on_catchall_scores_below_threshold():
    """The core safety property: a guess on a catch-all domain is never queued."""
    v = FakeVerifier(catchall=True)
    verdict = v.verify("stefan@acme.ai", source=SRC_INFERRED)
    assert verdict.status == CATCHALL
    assert verdict.confidence < 70


def test_published_address_on_catchall_is_still_usable():
    v = FakeVerifier(catchall=True)
    verdict = v.verify("stefan@acme.ai", source=SRC_SCRAPED)
    assert verdict.status == CATCHALL
    assert verdict.confidence >= 70


def test_catchall_domain_refuses_all_candidates():
    v = FakeVerifier(catchall=True)
    verdicts = v.verify_candidates(["a@acme.ai", "b@acme.ai"], "acme.ai")
    assert all(x.status == CATCHALL and x.confidence < 70 for x in verdicts.values())


def test_never_send_local_parts_are_refused_before_any_probe():
    v = FakeVerifier(catchall=True)
    assert v.verify("postmaster@acme.ai").confidence == 0
    assert v.verify("abuse@acme.ai").status == UNDELIVERABLE


def test_disposable_domains_are_refused():
    v = FakeVerifier(accepts={"x@mailinator.com"})
    assert v.verify("x@mailinator.com").status == UNDELIVERABLE


# ------------------------------------------------- exhaustive pattern search

def test_full_pattern_set_covers_common_styles():
    cands = candidate_addresses("Ada", "Lovelace", "acme.ai")
    locals_ = [c.split("@")[0] for c in cands]
    for expected in ["ada", "ada.lovelace", "adalovelace", "adal",
                     "alovelace", "ada_lovelace", "a.lovelace", "lovelace", "al"]:
        assert expected in locals_, expected
    assert len(cands) == len(set(cands))


def test_first_verified_short_circuits_on_the_first_hit():
    v = FakeVerifier(accepts={"alovelace@acme.ai"})
    found, verdict = v.first_verified(
        candidate_addresses("Ada", "Lovelace", "acme.ai"), "acme.ai"
    )
    assert found == "alovelace@acme.ai"
    assert verdict.status == VERIFIED


def test_first_verified_prefers_the_earlier_pattern():
    v = FakeVerifier(accepts={"ada@acme.ai", "alovelace@acme.ai"})
    found, _ = v.first_verified(
        candidate_addresses("Ada", "Lovelace", "acme.ai"), "acme.ai"
    )
    assert found == "ada@acme.ai"


def test_first_verified_reports_exhaustion():
    v = FakeVerifier(accepts=set())
    found, verdict = v.first_verified(
        candidate_addresses("Ada", "Lovelace", "acme.ai"), "acme.ai"
    )
    assert found is None
    assert verdict.status == UNDELIVERABLE
    assert "no pattern accepted" in verdict.detail


def test_first_verified_refuses_to_guess_on_catchall():
    """No number of patterns can prove anything when every address is accepted."""
    v = FakeVerifier(catchall=True)
    found, verdict = v.first_verified(
        candidate_addresses("Ada", "Lovelace", "acme.ai"), "acme.ai"
    )
    assert found is None
    assert verdict.status == CATCHALL
    assert verdict.confidence < 70


def test_chunking_still_finds_a_late_pattern():
    v = FakeVerifier(accepts={"lovelace.ada@acme.ai"})
    cands = candidate_addresses("Ada", "Lovelace", "acme.ai")
    found, _ = v.first_verified(cands, "acme.ai", chunk=3)
    assert found == "lovelace.ada@acme.ai"


def test_no_mx_host_does_not_crash_the_probe():
    """A domain with no MX must short-circuit, not reach smtplib with host=None."""
    real = Verifier(db=None)
    assert real._rcpt(None, ["a@acme.ai"]) == {"a@acme.ai": (0, b"no MX host")}
    assert real._rcpt("", ["a@acme.ai"]) == {"a@acme.ai": (0, b"no MX host")}


class SilentVerifier(FakeVerifier):
    """Server that never answers, e.g. a timeout or a throttled connection."""

    def _rcpt(self, host, addresses):
        return {addr: (0, b"timed out") for addr in addresses}


def test_silent_server_is_inconclusive_not_a_rejection():
    """A connection that never answers must not be recorded as 'no such user'."""
    v = SilentVerifier()
    found, verdict = v.first_verified(
        candidate_addresses("Ada", "Lovelace", "acme.ai"), "acme.ai"
    )
    assert found is None
    assert verdict.status == UNVERIFIED
    assert "no SMTP response" in verdict.detail


# --------------------------- catch-all test fused into the candidate session

class CountingVerifier(FakeVerifier):
    """Counts SMTP sessions so we can prove the probe is not duplicated."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.sessions = 0

    def _rcpt(self, host, addresses):
        self.sessions += 1
        return super()._rcpt(host, addresses)


def test_catchall_detected_without_a_second_connection():
    v = CountingVerifier(catchall=True)
    found, verdict = v.first_verified(
        candidate_addresses("Ada", "Lovelace", "acme.ai"), "acme.ai"
    )
    assert found is None and verdict.status == CATCHALL
    assert v.sessions == 1, "catch-all probe should ride along with the candidates"


def test_verification_uses_one_session_for_all_patterns():
    v = CountingVerifier(accepts={"alovelace@acme.ai"})
    found, _ = v.first_verified(
        candidate_addresses("Ada", "Lovelace", "acme.ai"), "acme.ai"
    )
    assert found == "alovelace@acme.ai"
    assert v.sessions == 1


def test_catchall_result_is_cached_for_the_domain():
    v = CountingVerifier(catchall=True)
    cands = candidate_addresses("Ada", "Lovelace", "acme.ai")
    v.first_verified(cands, "acme.ai")
    v.first_verified(cands, "acme.ai")
    assert v.sessions == 1, "second call should use the cached catch-all verdict"


# ------------------------- provenance when the server refuses to answer

class TimeoutVerifier(FakeVerifier):
    """Server that never answers, as when our IP is being throttled."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.calls = 0

    def _rcpt(self, host, addresses):
        if self.throttled:
            return {a: (0, b"paused") for a in addresses}
        self.calls += 1
        self.timeouts += 1
        if self.timeouts >= self.TIMEOUT_BUDGET:
            self.throttled = True
        return {a: (0, b"timed out") for a in addresses}


def test_published_address_survives_an_unanswered_probe():
    """Provenance is evidence: a human published this, the probe just failed."""
    v = TimeoutVerifier()
    verdict = v.verify("aidan@latch.bio", source=SRC_SCRAPED)
    assert verdict.status == UNVERIFIED
    assert verdict.confidence >= 70, "must stay above the send threshold"


def test_published_role_address_stays_below_threshold_when_unanswered():
    v = TimeoutVerifier()
    verdict = v.verify("hiring@latch.bio", source=SRC_SCRAPED)
    assert verdict.confidence < 70


def test_guessed_address_gains_nothing_from_an_unanswered_probe():
    """A guess with no answer is still a guess and must not be sent."""
    v = TimeoutVerifier()
    verdict = v.verify("aidan@latch.bio", source=SRC_INFERRED)
    assert verdict.confidence < 70


def test_repeated_timeouts_pause_probing():
    v = TimeoutVerifier()
    for i in range(v.TIMEOUT_BUDGET):
        v.verify(f"a{i}@acme.ai", source=SRC_INFERRED)
    assert v.throttled
    before = v.calls
    v.verify("later@acme.ai", source=SRC_INFERRED)
    assert v.calls == before, "must stop probing once throttled"


# The template opens "Dear {first_name},". A contact scraped without a name
# once rendered "Dear there," and three of those reached real inboxes, so the
# recovery path and, more importantly, its refusals are pinned here.
@pytest.mark.parametrize("email,expected", [
    ("omar@inkeep.com", "Omar"),
    ("austin+hn@krea.ai", "Austin"),          # plus-tag stripped
    ("john.smith@x.io", "John"),              # surname stripped
    ("MORGAN@experiencedevin.com", "Morgan"), # case normalised
])
def test_first_name_recovered_from_address(email, expected):
    assert first_name_from_email(email) == expected


@pytest.mark.parametrize("email", [
    "kk@datrics.ai",            # initials, not a name
    "gp@attack.capital",        # initials
    "carehub@hellocozmo.ai",    # shared inbox absent from ROLE_LOCALS
    "hiring@qualgent.ai",       # role account
    "support@acme.com",         # role account
    "a1b2@acme.com",            # not alphabetic
])
def test_first_name_declines_when_not_a_person(email):
    assert first_name_from_email(email) == ""
