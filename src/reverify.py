"""Repairing verdicts that recorded a failed probe rather than a mailbox.

When the prober stopped answering, every address it was holding came out as
"no SMTP answer, trusting published source" at 70, or "inconclusive" at 40.
Neither is a finding. They are the shape of a verdict the prober never reached,
and they are indistinguishable from a real one once written down.

The damage runs two ways, so the repair does too:

  contacts   re-probe the addresses whose verdict is one of those non-findings.
             Most come back verified at 95, some catch-all at 75, and a few are
             genuinely undeliverable and get suppressed so nothing ever writes
             to them.
  companies  a company parked as "no candidate address accepted" during an
             outage was never actually tested. Those go back to status=new for
             the ordinary enrich stage to try again.

Only contacts that have never been written to are touched. Anyone already
mailed keeps their status untouched, and nothing here can put a second message
in front of someone who has already had one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import UNDELIVERABLE
from .verifier import Verifier


@dataclass
class ReverifyReport:
    improved: list[tuple[str, int, int, str]] = field(default_factory=list)
    unchanged: list[tuple[str, int, str]] = field(default_factory=list)
    dropped: list[tuple[str, str]] = field(default_factory=list)
    examined: int = 0
    cache_cleared: int = 0
    companies_reopened: int = 0
    drafts_pulled: int = 0

    def rows(self) -> list[tuple]:
        return ([(e, f"{was} -> {now}", "improved", why[:40])
                 for e, was, now, why in self.improved]
                + [(e, str(conf), "unchanged", why[:40])
                   for e, conf, why in self.unchanged]
                + [(e, "0", "suppressed", why[:40]) for e, why in self.dropped])


def reverify_contacts(db, verifier: Verifier, limit: int = 0) -> ReverifyReport:
    """Re-probe pending contacts whose verdict recorded a failed probe.

    Ordered by priority then confidence, so a --limit run repairs the contacts
    closest to being mailed first.
    """
    report = ReverifyReport()
    for row in db.contacts_needing_reverify(limit):
        if verifier.throttled:
            # Carrying on would only rewrite one bad verdict as another, and
            # this time we would have no record that the probe was the problem.
            print("reverify: probing is being throttled, stopping here. "
                  "Re-run later to finish the rest.")
            break
        report.examined += 1
        was = int(row["confidence"])
        verdict = verifier.verify(row["email"], source=row["source"])
        if verdict.status == UNDELIVERABLE or verdict.confidence <= 0:
            # A server that names the address as unknown has told us something
            # the first probe never learned. Suppress rather than merely
            # downgrading, so no later change of threshold can revive it.
            db.suppress(row["email"], f"undeliverable: {verdict.detail}"[:120])
            # A queued contact already has a draft written and very likely a
            # copy in Gmail Drafts. Suppression stops the scheduled send on its
            # own; this pulls the draft so the Gmail copy gets cleaned up too.
            report.drafts_pulled += db.drop_draft_for_contact(row["id"])
            report.dropped.append((row["email"], verdict.detail))
            continue
        db.update_verification(row["id"], verdict.status, verdict.detail,
                               verdict.confidence)
        if verdict.confidence > was:
            report.improved.append(
                (row["email"], was, verdict.confidence, verdict.detail))
        else:
            report.unchanged.append((row["email"], verdict.confidence, verdict.detail))
    return report


def recover(db, verifier: Verifier, limit: int = 0,
            reopen: bool = True) -> ReverifyReport:
    """The whole repair: clear poisoned cache, re-probe, reopen companies.

    Cache first. Those rows are what the probes would otherwise be read
    through, and a domain remembered as "not a catch-all" on no evidence would
    send this run straight back down the path that created the mess.
    """
    cleared = db.clear_unproven_catchall()
    report = reverify_contacts(db, verifier, limit)
    report.cache_cleared = cleared
    if reopen:
        report.companies_reopened = db.reopen_for_enrich()
    return report
