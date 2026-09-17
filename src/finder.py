"""Company discovery and contact enrichment, entirely on free sources.

Discovery reads the YC OSS mirror of the public Y Combinator directory, which
is a daily-refreshed JSON feed with no key and no quota. It carries everything
the target filter needs: location, team size, batch (a proxy for age), active
status, hiring flag, and a description we reuse for personalisation.

Enrichment then does two things per company: read the public YC page for
founder names, roles and bios, and crawl a few pages of the company site for
any address it publishes. Guessed addresses are only ever trusted when the
domain actually rejects unknown recipients; see verifier.py for why.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

from .models import (
    CATCHALL, Company, Contact, ENRICHED, NO_CONTACTS, PENDING, SRC_INFERRED,
    SRC_SCRAPED, VERIFIED,
)
from . import patterns
from .github_source import find_org_for
from .patterns import apply_pattern
from .scraper import Fetcher, Person, parse_yc_founders, scrape_site
from .verifier import Verifier, candidate_addresses, is_never_send, is_role_account

YC_TAG_URL = "https://yc-oss.github.io/api/tags/{tag}.json"

# Tags on the YC feed that mean "this is an ML/AI company".
AI_TAGS = (
    "ai", "artificial-intelligence", "machine-learning", "ml", "deep-learning",
    "generative-ai", "conversational-ai", "reinforcement-learning",
    "ai-assistant", "aiops",
)

BATCH_YEAR = re.compile(r"(\d{4})")

# Team size we most want to reach: big enough to be hiring, small enough that
# a founder still reads their own inbox.
IDEAL_TEAM = 25


def domain_from_url(url: str) -> str:
    try:
        netloc = urlparse(url if url.startswith("http") else "https://" + url).netloc
    except Exception:
        return ""
    return netloc.replace("www.", "").lower().strip()


def batch_year(batch: str) -> int:
    match = BATCH_YEAR.search(batch or "")
    return int(match.group(1)) if match else 0


# Season ordering within a year, so Fall 2026 outranks Winter 2026.
BATCH_SEASONS = {"winter": 0, "spring": 1, "summer": 2, "fall": 3}


def batch_recency(batch: str) -> float:
    """How recently this company was funded, as a sortable number.

    A YC batch is a funding event: every company in it took the standard cheque
    on joining. That makes batch date the one recent-funding signal available
    for free and without name matching, which is where SEC Form D lookups fall
    down. Higher is more recent.
    """
    year = batch_year(batch)
    if not year:
        return 0.0
    season = next((v for k, v in BATCH_SEASONS.items()
                   if k in (batch or "").lower()), 0)
    return year + season / 4.0


def size_fit(team: int | None) -> float:
    """How likely a team of this size is to actually take on an intern.

    Under about eight people there is usually no capacity to supervise one;
    past roughly eighty the founder is behind a recruiting process. The middle
    is where a cold note reaches someone who can say yes.
    """
    if not team:
        return 0.0
    if 10 <= team <= 60:
        return 2.0
    if 5 <= team <= 100:
        return 1.0
    if team < 5:
        return -1.0
    return 0.0


def funding_score(record: dict, now_year: int) -> float:
    """Blend recency of funding with whether the company can hire.

    Deliberately a weighted sum rather than a sort by recency then size: money
    that landed last month is worthless if the company is two people who cannot
    supervise anyone. Recency is the strongest single term, not an override.
    """
    recency = batch_recency(record.get("batch", ""))
    if not recency:
        return -10.0
    years_ago = max(0.0, (now_year + 0.75) - recency)
    if years_ago <= 0.5:
        points = 3.0          # funded within roughly the last six months
    elif years_ago <= 1.0:
        points = 2.0
    elif years_ago <= 2.0:
        points = 1.0
    else:
        points = 0.0
    points += size_fit(record.get("team_size"))
    if record.get("isHiring"):
        points += 1.5
    if (record.get("stage") or "").lower() == "early":
        points += 0.5
    # Recency still breaks ties among equally suitable companies.
    return points + recency / 10000.0


@dataclass
class DiscoveryStats:
    fetched: int = 0
    considered: int = 0
    qualified: int = 0
    already_known: int = 0
    added: int = 0


def _matches_location(location: str, targets: tuple[str, ...]) -> str:
    low = (location or "").lower()
    for target in targets:
        if target and target in low:
            return target.title()
    return ""


def qualifies(record: dict, settings, now_year: int,
              priority=None) -> tuple[bool, str]:
    """Apply the target criteria to one raw YC record.

    A company named in the priority list skips the criteria. Every one of them
    is a proxy for "is this place worth writing to", and an explicit naming is
    better evidence than the proxies. The two checks that survive are not
    preferences: a dead company cannot hire, and a company with no website
    gives the enrich stage nothing to work with.
    """
    if record.get("status") != "Active":
        return False, "inactive"
    if not record.get("website"):
        return False, "no website"
    if priority is not None and priority.has(name=record.get("name", ""),
                                             domain=domain_from_url(record["website"])):
        return True, ""
    if not _matches_location(record.get("all_locations", ""), settings.target_locations):
        return False, "location"
    team = record.get("team_size") or 0
    if not 0 < team <= settings.max_employees:
        return False, f"team_size {team}"
    year = batch_year(record.get("batch", ""))
    if year and now_year - year > settings.max_company_age_years:
        return False, f"batch {year} too old"
    if not year:
        return False, "unknown batch"
    if settings.hiring_only and not record.get("isHiring"):
        return False, "not hiring"
    return True, ""


def discover(fetcher: Fetcher, settings, db, limit: int = 15,
             priority=None) -> tuple[list[Company], DiscoveryStats]:
    """Return up to `limit` qualifying companies not already in the database."""
    stats = DiscoveryStats()
    raw: dict[int, dict] = {}
    for tag in AI_TAGS:
        payload = fetcher.get_json(YC_TAG_URL.format(tag=tag))
        if not payload:
            continue
        stats.fetched += 1
        for record in payload:
            raw[record["id"]] = record

    now_year = datetime.now().year
    found: list[Company] = []
    # Hiring first, then closest to IDEAL_TEAM. A two-person company rarely has
    # a role to fill; a 150-person one rarely has a reachable founder.
    if getattr(settings, "prefer_recently_funded", True):
        # Freshly funded first, then closest to a reachable team size.
        ordered = sorted(
            raw.values(),
            key=lambda r: (-funding_score(r, now_year),
                           abs((r.get("team_size") or 9999) - IDEAL_TEAM)),
        )
    else:
        ordered = sorted(
            raw.values(),
            key=lambda r: (not r.get("isHiring"),
                           abs((r.get("team_size") or 9999) - IDEAL_TEAM)),
        )
    for record in ordered:
        stats.considered += 1
        ok, _reason = qualifies(record, settings, now_year, priority)
        if not ok:
            continue
        stats.qualified += 1
        domain = domain_from_url(record["website"])
        if not domain:
            continue
        # Snapshot before the dedupe skip: growth is only measurable if we keep
        # logging companies we already know about.
        db.record_snapshot(domain, record.get("team_size"))
        if db.company_exists(domain):
            stats.already_known += 1
            continue
        found.append(Company(
            name=record["name"],
            domain=domain,
            website=record["website"],
            location=record.get("all_locations", "").split(";")[0].strip(),
            industry=record.get("industry", ""),
            one_liner=record.get("one_liner", ""),
            description=(record.get("long_description") or "")[:1200],
            batch=record.get("batch", ""),
            founded_year=batch_year(record.get("batch", "")) or None,
            employees=record.get("team_size"),
            is_hiring=bool(record.get("isHiring")),
            yc_url=record.get("url", ""),
            source="yc",
        ))
        if len(found) >= limit:
            break
    stats.added = len(found)
    return found, stats


def role_matches(person: Person, targets: tuple[str, ...]) -> bool:
    blob = f"{person.role} {person.bio}".lower()
    return any(target in blob for target in targets)


@dataclass
class EnrichResult:
    contacts: list[Contact]
    note: str = ""


def _row_get(row, key: str, default: str = "") -> str:
    """Read an optional column from a sqlite3.Row or a plain mapping."""
    try:
        return row[key] or default
    except (IndexError, KeyError, TypeError):
        return default


def enrich_company(company_row, fetcher: Fetcher, verifier: Verifier,
                   settings, github=None) -> EnrichResult:
    """Find the best reachable person at one company."""
    domain = company_row["domain"]
    company_id = company_row["id"]
    contacts: list[Contact] = []
    gh_pattern = ""

    # 1. Founder names, roles and bios from the public YC page.
    people: list[Person] = []
    html = ""  # reused below for GitHub org detection, so bind it unconditionally
    if company_row["yc_url"]:
        html = fetcher.get(company_row["yc_url"], respect_robots=False)
        if html:
            people = parse_yc_founders(html)
    targeted = [p for p in people if role_matches(p, settings.target_roles)] or people

    # 2. Addresses the company publishes itself.
    scrape = scrape_site(fetcher, company_row["website"], domain)
    published = {e for e in scrape.emails if not is_never_send(e)}

    # 2b. Addresses the team published in git commits. These are written by the
    # person, not inferred, so they survive a catch-all domain. The observed
    # naming pattern is reused below when nothing else pans out.
    if github is not None and not github.exhausted:
        # A company discovered through GitHub already knows its org. Rediscovery
        # costs a call and can resolve to a different account with a similar
        # name, so the recorded slug wins when there is one.
        org = _row_get(company_row, "github_org") or find_org_for(
            fetcher, company_row["website"], html=html or "",
            company_name=company_row["name"], client=github)
        if org:
            findings = github.harvest(org, domain)
            gh_pattern = findings.pattern
            published |= {e for e in findings.emails if not is_never_send(e)}

    # 3. A published personal address is the single best outcome.
    for email in sorted(published):
        if is_role_account(email):
            continue
        verdict = verifier.verify(email, source=SRC_SCRAPED)
        if verdict.confidence <= 0:
            continue
        first, last = _match_person(email, targeted)
        if first:
            # Every address tied to a real name teaches us this domain's
            # convention, which is what makes later guesses cheap and safe.
            patterns.learn(verifier.db, email, first, last, "published")
        # An address we can tie to a named founder beats an anonymous one, so it
        # wins the sort below rather than losing to alphabetical order.
        confidence = verdict.confidence + (5 if first else 0)
        contacts.append(Contact(
            email=email, company_id=company_id, first_name=first, last_name=last,
            role=_role_for(first, targeted), source=SRC_SCRAPED,
            verify_status=verdict.status, verify_detail=verdict.detail,
            confidence=min(confidence, 99), status=PENDING,
            bio=_bio_for(first, targeted),
        ))

    # 4. Otherwise guess from a founder name, but only where a guess is provable.
    # first_verified folds the catch-all test into the same SMTP session, so we
    # deliberately do not pre-check is_catchall here; that would cost an extra
    # connection per company, which is the slowest thing in the pipeline.
    if not contacts and targeted:
        for person in targeted[:3]:
            # Ordered by what we have learned: this domain's own convention
            # first, then the corpus-wide ordering. Fewer probes per hit.
            candidates = patterns.ordered_candidates(
                verifier.db, person.first_name, person.last_name, domain)
            if not candidates:
                continue
            found, verdict = verifier.first_verified(candidates, domain)
            if verdict.status == CATCHALL:
                break  # unprovable for everyone at this domain, not just this person
            if found:
                patterns.learn(verifier.db, found, person.first_name,
                               person.last_name, "verified")
                contacts.append(Contact(
                    email=found, company_id=company_id,
                    first_name=person.first_name, last_name=person.last_name,
                    role=person.role, bio=person.bio, linkedin=person.linkedin,
                    source=SRC_INFERRED, verify_status=verdict.status,
                    verify_detail=verdict.detail, confidence=verdict.confidence,
                    status=PENDING,
                ))
                break

    # 4b. On a catch-all domain a guess is normally unprovable, but if the
    # team's own commits show the house pattern we are applying observed
    # evidence rather than guessing blindly.
    # Prefer a convention learned from this domain's own confirmed addresses;
    # fall back to whatever the commit history showed this run.
    learned, evidence = patterns.known_pattern(verifier.db, domain)
    house_pattern = learned or gh_pattern
    if not contacts and house_pattern and targeted and verifier.is_catchall(domain):
        for person in targeted:
            built = apply_pattern(house_pattern, person.first_name,
                                  person.last_name, domain)
            if not built or is_never_send(built):
                continue
            contacts.append(Contact(
                email=built, company_id=company_id,
                first_name=person.first_name, last_name=person.last_name,
                role=person.role, bio=person.bio, linkedin=person.linkedin,
                source=SRC_INFERRED, verify_status=CATCHALL,
                verify_detail=(f"built from the {house_pattern} convention observed "
                               f"on {domain}"),
                confidence=75, status=PENDING,
            ))
            break

    # 5. Last resort: a published role address such as hello@ or founders@.
    if not contacts:
        for email in sorted(published):
            if not is_role_account(email):
                continue
            verdict = verifier.verify(email, source=SRC_SCRAPED)
            if verdict.confidence <= 0:
                continue
            contacts.append(Contact(
                email=email, company_id=company_id, source=SRC_SCRAPED,
                role="team inbox", verify_status=verdict.status,
                verify_detail=verdict.detail, confidence=verdict.confidence,
                status=PENDING,
            ))
            break

    contacts.sort(key=lambda c: -c.confidence)
    note = "" if contacts else _why_empty(people, published, verifier, domain)
    return EnrichResult(contacts=contacts[:1], note=note)


def _why_empty(people, published, verifier: Verifier, domain: str) -> str:
    # Check MX first: a domain with no MX cannot receive mail at all, and
    # reporting that as "no pattern accepted" hides the real reason.
    if not verifier.mx_host(domain):
        return "domain has no MX record, cannot receive mail"
    if not people and not published:
        return "no founders listed and no published address"
    if people and verifier.is_catchall(domain):
        return "catch-all domain, guesses unprovable"
    if people:
        return "no candidate address accepted"
    return "no usable address"


def _match_person(email: str, people: list[Person]) -> tuple[str, str]:
    """Tie a published address back to a named founder when the local part fits."""
    local = email.split("@", 1)[0].lower()
    for person in people:
        first = person.first_name.lower()
        last = person.last_name.lower()
        if not first:
            continue
        if local == first or local.startswith(f"{first}.") or (
            last and local in {f"{first}{last}", f"{first}.{last}", f"{first[0]}{last}"}
        ):
            return person.first_name, person.last_name
    return "", ""


def _role_for(first: str, people: list[Person]) -> str:
    for person in people:
        if person.first_name == first and first:
            return person.role
    return ""


def _bio_for(first: str, people: list[Person]) -> str:
    for person in people:
        if person.first_name == first and first:
            return person.bio
    return ""
