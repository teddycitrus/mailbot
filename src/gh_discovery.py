"""GitHub as a company discovery source, not only an enrichment one.

YC is one accelerator, and its directory carries its own geography: 206 San
Francisco companies against 7 in Toronto. That ratio describes YC, not the
market. GitHub org search is keyed on a self-reported location, so it reaches
companies that never applied to an accelerator, and it is the only free source
that covers all three target cities with comparable depth.

Honest limitation, the same one `hn_source` carries. A YC record has a batch and
a team size, so the under-five-years and under-200-people filters run properly.
A GitHub org has neither: org creation date stands in for founding year, which
is a proxy, and headcount is unknown. Companies land with source='github' so
they stay distinguishable from the properly filtered YC set.

Cost model. The search endpoint returns logins only, so learning a company's
website costs one profile call each. The search query therefore does as much
filtering as it can server side, and `discover` stops at the first `limit` new
companies rather than walking the whole result set.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from itertools import zip_longest
from urllib.parse import urlparse

from .models import Company

# GitHub matches the location field as free text, and people write the same
# city several ways. One alias set per target keeps those spellings reachable
# without a second pass over the whole index.
LOCATION_ALIASES: dict[str, tuple[str, ...]] = {
    "san francisco": ("San Francisco", "SF Bay Area", "Bay Area"),
    "new york": ("New York", "NYC", "Brooklyn"),
    "toronto": ("Toronto", "Ontario, Canada"),
}

# Hosting, publishing and link-in-bio services. An org whose only website is one
# of these has no company domain, so there is nothing to send mail to.
NOT_COMPANY_HOSTS = {
    "github.io", "github.com", "gitlab.io", "readthedocs.io", "gitbook.io",
    "herokuapp.com", "vercel.app", "netlify.app", "pages.dev", "web.app",
    "firebaseapp.com", "surge.sh", "glitch.me", "repl.co",
    "medium.com", "substack.com", "wordpress.com", "blogspot.com",
    "wixsite.com", "squarespace.com", "notion.site", "notion.so",
    "linktr.ee", "carrd.co", "about.me", "bio.link",
    "twitter.com", "x.com", "linkedin.com", "facebook.com", "youtube.com",
    "discord.gg", "discord.com", "slack.com", "t.me",
}

# Universities and government publish research code under org accounts. They are
# not startups and they do not hire interns the way this campaign assumes.
ACADEMIC_SUFFIXES = (".edu", ".gov", ".mil")
# Most of the world puts the sector in a second level: ac.uk, edu.au, gov.uk.
ACADEMIC_MARKERS = (".ac.", ".edu.", ".gov.")
# Canada does not, so its universities have to be named. Toronto is a target
# city, and a U of T lab is exactly the kind of org this source surfaces.
ACADEMIC_DOMAINS = {
    "utoronto.ca", "mcgill.ca", "uwaterloo.ca", "yorku.ca", "torontomu.ca",
    "ryerson.ca", "queensu.ca", "ubc.ca", "ualberta.ca", "mcmaster.ca",
    "uottawa.ca", "concordia.ca", "sfu.ca", "uwo.ca", "dal.ca", "ucalgary.ca",
}


@dataclass
class OrgStats:
    searched: int = 0
    scanned: int = 0
    looked_up: int = 0
    already_known: int = 0
    rejected: int = 0
    added: int = 0


def aliases_for(target: str) -> tuple[str, ...]:
    """Query spellings for one configured location."""
    return LOCATION_ALIASES.get(target.strip().lower(), (target.strip().title(),))


def domain_of(blog: str) -> str:
    """Normalised registrable-ish domain from an org's website field.

    Org profiles are hand typed, so the field arrives as 'acme.ai',
    'https://www.acme.ai/', or with a path attached.
    """
    raw = (blog or "").strip().lower()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    host = urlparse(raw).netloc.split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if "." not in host or host.endswith("."):
        return ""
    return host


def usable_domain(domain: str) -> bool:
    """Reject hosts that cannot belong to a company we could email."""
    if not domain:
        return False
    if domain.endswith(ACADEMIC_SUFFIXES) or any(m in domain for m in ACADEMIC_MARKERS):
        return False
    if any(domain == d or domain.endswith("." + d) for d in ACADEMIC_DOMAINS):
        return False
    if domain in NOT_COMPANY_HOSTS:
        return False
    # Subdomains of a hosting service are equally unusable: acme.github.io.
    return not any(domain.endswith("." + host) for host in NOT_COMPANY_HOSTS)


def build_query(alias: str, max_age_years: int, today: date | None = None) -> str:
    """Server-side filter, so the profile lookups are only spent on candidates.

    `created` is the org account's creation date. It is the closest free proxy
    for founding year, and it is conservative in the useful direction: a company
    older than its org account is rare, the reverse is common.
    """
    today = today or date.today()
    cutoff = today.replace(year=today.year - max_age_years).isoformat()
    return f'type:org location:"{alias}" created:>{cutoff} repos:>0'


def ordered_queries(targets: tuple[str, ...]) -> list[tuple[str, str]]:
    """Every (location, alias) pair, cities interleaved before aliases.

    Grouping by city instead puts all three San Francisco spellings ahead of
    Toronto's first one, and a small run limit is then spent before Toronto is
    ever queried. Round one asks each city once, round two asks each city's
    second spelling, and so on.
    """
    per_city = [[(t.title(), alias) for alias in aliases_for(t)] for t in targets]
    return [pair for round_ in zip_longest(*per_city) for pair in round_ if pair]


def to_company(profile: dict, location: str) -> Company | None:
    """Turn one org profile into a Company, or None if it does not qualify."""
    domain = domain_of(profile.get("blog", ""))
    if not usable_domain(domain):
        return None
    login = profile.get("login") or ""
    created = (profile.get("created_at") or "")[:4]
    bio = (profile.get("bio") or profile.get("description") or "").strip()
    return Company(
        name=(profile.get("name") or login).strip()[:60],
        domain=domain,
        website=f"https://{domain}",
        location=location,
        one_liner=bio[:180].replace("\n", " "),
        description=bio[:1200],
        founded_year=int(created) if created.isdigit() else None,
        source="github",
        github_org=login,
    )


def discover(client, settings, db, limit: int = 40,
             max_pages: int = 3, today: date | None = None
             ) -> tuple[list[Company], OrgStats]:
    """Up to `limit` qualifying orgs across the configured locations.

    Locations are interleaved rather than drained in order. Searching San
    Francisco to exhaustion first would spend the whole limit there and leave
    Toronto at zero, which is the failure this source exists to fix.
    """
    stats = OrgStats()
    found: list[Company] = []
    seen_logins: set[str] = set()
    seen_domains: set[str] = set()

    queries = ordered_queries(settings.target_locations)
    if not queries:
        return found, stats

    # Results are buffered per query and drained a few at a time, so a page of
    # a hundred San Francisco orgs is not thrown away just because the round
    # moved on to New York.
    buffers: dict[str, list[dict]] = {alias: [] for _, alias in queries}
    pages: dict[str, int] = {alias: 0 for _, alias in queries}
    drained: set[str] = set()
    chunk = max(1, limit // len(queries))

    while len(found) < limit and not client.exhausted:
        progressed = False
        for location, alias in queries:
            if len(found) >= limit or client.exhausted:
                break
            taken = 0
            while taken < chunk and len(found) < limit and not client.exhausted:
                if not buffers[alias]:
                    if alias in drained or pages[alias] >= max_pages:
                        break
                    pages[alias] += 1
                    items = client.search_orgs(
                        build_query(alias, settings.max_company_age_years, today),
                        per_page=100, page=pages[alias],
                    )
                    stats.searched += 1
                    progressed = True
                    if not items:
                        drained.add(alias)
                        break
                    buffers[alias] = items

                item = buffers[alias].pop(0)
                progressed = True
                login = item.get("login") or ""
                if not login or login in seen_logins:
                    continue
                seen_logins.add(login)
                stats.scanned += 1

                profile = client.get_user(login)
                stats.looked_up += 1
                company = to_company(profile, location)
                if company is None:
                    stats.rejected += 1
                    continue
                if company.domain in seen_domains or db.company_exists(company.domain):
                    stats.already_known += 1
                    continue
                seen_domains.add(company.domain)
                found.append(company)
                stats.added += 1
                taken += 1
        if not progressed:
            break
    return found, stats
