"""Typed records passed between pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Company.status
NEW = "new"
ENRICHED = "enriched"
NO_CONTACTS = "no_contacts"
SKIPPED = "skipped"

# Contact.status
PENDING = "pending"
QUEUED = "queued"
SENT = "sent"
REPLIED = "replied"
BOUNCED = "bounced"
SUPPRESSED = "suppressed"

# Contact.verify_status
UNVERIFIED = "unverified"
VERIFIED = "verified"          # mailbox confirmed by SMTP RCPT
CATCHALL = "catchall"          # domain accepts everything; cannot confirm
UNDELIVERABLE = "undeliverable"
NO_MX = "no_mx"


# Contact.provenance: how the address was come by. On a catch-all domain the
# SMTP probe proves nothing, so this is the only evidence the address is real.
PROV_HN_POST = "hn_post"                      # the person posted it on HN
PROV_FOUNDER_PUBLISHED = "founder_published"  # published, local part fits a named founder
PROV_GIT_COMMIT = "git_commit"                # author address on the company's commits
PROV_HOUSE_PATTERN = "house_pattern"          # founder name + convention proven on the domain
PROV_SITE = "site_unattributed"               # published on the site, tied to nobody
PROV_GUESS = "guess"                          # a permutation of a founder's name
# Sources good enough to send to on a catch-all domain. Unattributed site
# addresses bounced 8 in 23 there; these bounced 4 in 75 between them.
TRUSTED_PROVENANCE = frozenset({
    PROV_HN_POST, PROV_FOUNDER_PUBLISHED, PROV_GIT_COMMIT, PROV_HOUSE_PATTERN,
})
# The strongest of those: the person wrote the address down themselves.
# What is left of catch-all sending when the bounce rate climbs (0 bounces
# in 49 so far); inferred addresses go first. See Settings.catchall_provenance.
SELF_PUBLISHED = frozenset({PROV_HN_POST, PROV_FOUNDER_PUBLISHED})


def deliverable(verify_status: str, provenance: str) -> bool:
    """Mail goes only to a mailbox that accepted an SMTP RCPT, or, on a
    catch-all domain where no probe can tell, to an address from a trusted
    source."""
    if verify_status == VERIFIED:
        return True
    return verify_status == CATCHALL and provenance in TRUSTED_PROVENANCE

# Company.skip_reason. Only the ones the rest of the code has to match on: the
# first is a finding about the domain, the others record a lookup or probe
# that never answered, and telling them apart is what keeps an outage from
# retiring a company for good. See Database.RETRYABLE_SKIPS.
SKIP_NO_MX = "domain has no MX record, cannot receive mail"
SKIP_MX_UNRESOLVED = "MX lookup failed, DNS did not answer"
# Founders were found but no mail server answered a single probe, usually
# because the laptop relay was off. Nothing was learned about the addresses.
SKIP_PROBE_UNAVAILABLE = "SMTP probing unavailable, retry when it is back"
# Addresses were found, but no mailbox among them accepted an SMTP RCPT.
SKIP_NOT_VERIFIED = "no verified or trusted address"

# Where an address came from, best first.
SRC_SCRAPED = "scraped"        # literally published on the site or YC page
SRC_INFERRED = "inferred"      # built from a founder name plus a domain pattern


@dataclass
class Company:
    name: str
    domain: str
    website: str = ""
    location: str = ""
    industry: str = ""
    one_liner: str = ""
    description: str = ""
    batch: str = ""
    founded_year: Optional[int] = None
    employees: Optional[int] = None
    is_hiring: bool = False
    yc_url: str = ""
    github_org: str = ""
    # Named in config/priority.txt: skips the size and age gates, and sorts
    # ahead of everything else from enrichment onward.
    priority: int = 0
    source: str = "yc"
    status: str = NEW
    skip_reason: str = ""
    id: Optional[int] = None


@dataclass
class Contact:
    email: str
    company_id: Optional[int] = None
    first_name: str = ""
    last_name: str = ""
    role: str = ""
    bio: str = ""
    linkedin: str = ""
    confidence: int = 0
    source: str = SRC_SCRAPED
    verify_status: str = UNVERIFIED
    verify_detail: str = ""
    status: str = PENDING
    provenance: str = ""
    id: Optional[int] = None

    @property
    def full_name(self) -> str:
        return " ".join(part for part in (self.first_name, self.last_name) if part)

    @property
    def display_name(self) -> str:
        return self.first_name or self.email.split("@", 1)[0]

    @property
    def domain(self) -> str:
        return self.email.split("@", 1)[1].lower() if "@" in self.email else ""

    @property
    def is_role_account(self) -> bool:
        local = self.email.split("@", 1)[0].lower()
        return local in ROLE_LOCALS


ROLE_LOCALS = {
    "hello", "hi", "info", "contact", "support", "sales", "team", "founders",
    "admin", "help", "careers", "jobs", "press", "hiring", "recruiting",
    "people", "talent", "general", "inquiries", "office",
    # Shared inboxes that read like a person but are not one.
    "join", "apply", "work", "hr", "recruit", "opportunities", "hey",
    "welcome", "reachus", "connect", "email", "mail", "enquiries",
}


@dataclass
class Draft:
    contact_id: int
    subject: str
    body: str
    to_email: str
    to_name: str = ""
    attachments: list[str] = field(default_factory=list)
    id: Optional[int] = None


@dataclass
class SendResult:
    ok: bool
    message_id: str = ""
    error: str = ""
    backend: str = ""
