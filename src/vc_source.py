"""VC portfolio pages as a company discovery source.

YC is one accelerator. The funds in config/vc_portfolios.txt, US and Canadian,
publish their portfolios as public pages, and each page names companies with a
link to their site. That link is all this needs: the domain becomes a company
row with source='vc', and the existing enrich stage takes it from there.

Two page shapes cover nearly every fund. Server-rendered pages carry plain
anchors to each company. Pages built on a headless CMS ship the same links
inside an embedded JSON payload instead, so every absolute URL in the page
source is read too, with CDNs, analytics and social hosts filtered out.

A portfolio also lists the fund's oldest wins: Airbnb, Coinbase, Atlassian.
Those are not startups that answer a cold intern email, and their founders are
not reachable. The free Tranco top-sites ranking separates them out: a domain
ranked inside VC_MAX_SITE_RANK is a big company and is skipped. Without the
ranking there is no way to tell, so discovery adds nothing rather than guess.

Like `gh_discovery`, a portfolio entry has no team size or founding year, so
the size and age gates cannot run; the rank filter is the stand-in.
"""

from __future__ import annotations

import io
import re
import time
import zipfile
from dataclasses import dataclass
from datetime import date
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .gh_discovery import usable_domain
from .models import Company

TRANCO_URL = "https://tranco-list.eu/top-1m.csv.zip"
TRANCO_MAX_AGE_DAYS = 7

ABSOLUTE_URL = re.compile(r"https?://([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
# "Acme | AI for invoices": the name is the part before the separator. The
# escapes are the en and em dash, which titles use as often as a hyphen.
TITLE_SEPARATOR = re.compile(r"\s+[|\-:\u2013\u2014]\s+")

# Infrastructure that shows up in every page source: CDNs, CMS APIs, fonts,
# analytics, schema vocabularies, app stores, job boards, social networks.
INFRA_HOSTS = {
    "cloudfront.net", "sanity.io", "w3.org", "schema.org", "ogp.me", "gmpg.org",
    "googletagmanager.com", "google-analytics.com", "googleapis.com",
    "gstatic.com", "google.com", "doubleclick.net", "hotjar.com", "segment.com",
    "segment.io", "hubspot.com", "hs-scripts.com", "hsforms.com", "typekit.net",
    "fontawesome.com", "jsdelivr.net", "unpkg.com", "cloudflare.com",
    "cloudflareinsights.com", "webflow.com", "webflow.io", "website-files.com",
    "squarespace-cdn.com", "wp.com", "wp.me", "w.org", "wordpress.org",
    "gravatar.com", "rankmath.com", "framer.com", "framerusercontent.com",
    "contentful.com", "ctfassets.net", "imgix.net", "vimeo.com", "vimeocdn.com",
    "youtube-nocookie.com", "spotify.com", "apple.com", "apps.apple.com",
    "play.google.com", "instagram.com", "tiktok.com", "threads.net",
    "angel.co", "wellfound.com", "crunchbase.com", "pitchbook.com",
    "getro.com", "greenhouse.io", "lever.co", "ashbyhq.com", "workable.com",
    "prnewswire.com", "businesswire.com", "techcrunch.com", "bloomberg.com",
    "forbes.com", "wsj.com", "nytimes.com", "wikipedia.org", "bit.ly", "t.co",
    "mailchimp.com", "list-manage.com", "calendly.com", "typeform.com",
    "paper.design", "vercel.link", "convertflow.co", "altareturn.com",
    "fiscloudservices.com", "intralinks.com", "cpanel.com", "cpanel.net",
}

# Two-label public suffixes, so app.acme.co.uk collapses to acme.co.uk and not
# to co.uk.
TWO_LEVEL_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "com.au", "com.br", "co.in", "co.jp", "co.il",
    "com.mx", "com.sg", "co.nz", "co.za", "com.tr", "com.cn", "qc.ca", "on.ca",
    "gc.ca", "gouv.fr",
}

# Anchor text that names the action, not the company.
GENERIC_LABELS = {
    "website", "visit", "visit website", "visit site", "learn more", "read more",
    "more", "site", "link", "view", "open", "go", "here", "external link",
}
# Words that only appear in a link's call to action or screen-reader hint.
ACTION_WORDS = re.compile(r"\b(website|visit|opens in|new tab|new window)\b", re.I)

# The campaign is US and Canadian. A two-letter top-level domain is a country,
# so anything outside these is a company abroad; the rest are country codes
# startups use generically (.io, .ai, .co).
HOME_TLDS = {"us", "ca"}
GENERIC_CCTLDS = {
    "io", "ai", "co", "me", "tv", "so", "sh", "gg", "ly", "fm", "to", "vc",
    "cc", "is", "it", "ac", "am", "la", "ws", "id",
}


@dataclass
class Portfolio:
    fund: str
    country: str
    url: str


@dataclass
class VcStats:
    pages: int = 0
    pages_failed: int = 0
    candidates: int = 0
    already_known: int = 0
    too_big: int = 0
    dead: int = 0
    moved: int = 0
    added: int = 0


def load_portfolios(path: Path) -> list[Portfolio]:
    """`fund | country | url` per line; # starts a comment."""
    if not path.exists():
        return []
    out: list[Portfolio] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 3 or not parts[2].startswith("http"):
            continue
        out.append(Portfolio(*parts))
    return out


def registrable(host: str) -> str:
    """The company's own domain from any host under it: jobs.acme.ai -> acme.ai."""
    host = host.lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    labels = host.split(".")
    if len(labels) < 2:
        return ""
    keep = 3 if ".".join(labels[-2:]) in TWO_LEVEL_SUFFIXES else 2
    return ".".join(labels[-keep:])


def _is_infra(domain: str) -> bool:
    return domain in INFRA_HOSTS


def _clean_label(text: str) -> str:
    text = " ".join(unescape(text or "").split())
    text = re.sub(r"\s*\blogo\b\s*$", "", text, flags=re.I).strip(" -|:")
    if not (2 <= len(text) <= 40) or len(text.split()) > 5:
        return ""
    if text.lower() in GENERIC_LABELS or ACTION_WORDS.search(text):
        return ""
    # A bare URL as the link text: "www.acme.ai/".
    if "/" in text or re.fullmatch(r"(www\.)?[\w-]+(\.[\w-]+)+", text):
        return ""
    return text


def in_target_market(domain: str) -> bool:
    tld = domain.rsplit(".", 1)[-1]
    if len(tld) != 2:
        return True
    return tld in HOME_TLDS or tld in GENERIC_CCTLDS


def extract_companies(html: str, page_url: str) -> dict[str, str]:
    """Company domain -> name as the page labels it ("" when it does not)."""
    own = registrable(urlparse(page_url).netloc)
    # The fund's sister sites (a16zcrypto.com for a16z.com) share its name.
    own_label = own.split(".")[0]
    found: dict[str, str] = {}

    def consider(host: str, label: str = "") -> None:
        domain = registrable(host)
        if not domain or domain == own or _is_infra(domain):
            return
        if len(own_label) >= 3 and domain.startswith(own_label):
            return
        if not usable_domain(domain) or not in_target_market(domain):
            return
        if label or domain not in found:
            found[domain] = label or found.get(domain, "")

    soup = BeautifulSoup(html, "lxml")
    for a in soup.find_all("a", href=True):
        href = urljoin(page_url, a["href"])
        if not href.startswith("http"):
            continue
        label = _clean_label(a.get_text(" ", strip=True))
        if not label:
            img = a.find("img", alt=True)
            label = _clean_label(img["alt"]) if img else ""
        if not label:
            label = _clean_label(a.get("title", ""))
        consider(urlparse(href).netloc, label)

    # Headless-CMS pages: the links are in a JSON payload, escaped as \/.
    for host in ABSOLUTE_URL.findall(html.replace("\\/", "/")):
        consider(host)
    return found


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def site_summary(html: str, domain: str = "") -> tuple[str, str]:
    """(name, description) from a company homepage's meta tags.

    Titles put the tagline first as often as the name ("Generative AI for
    Clinical Conversations | Abridge"), so the segment that matches the domain
    wins over position.
    """
    soup = BeautifulSoup(html, "lxml")

    def meta(*keys: str) -> str:
        for key in keys:
            tag = (soup.find("meta", attrs={"property": key})
                   or soup.find("meta", attrs={"name": key}))
            if tag and tag.get("content"):
                return " ".join(tag["content"].split())
        return ""

    title = soup.title.string.strip() if soup.title and soup.title.string else ""
    candidates = [meta("og:site_name"), *TITLE_SEPARATOR.split(title)]
    candidates = [c for c in (_clean_label(c) for c in candidates) if c]
    stem = _squash(domain.split(".")[0]) if domain else ""
    name = next((c for c in candidates
                 if stem and (_squash(c) in stem or stem in _squash(c))), "")
    if not name and candidates:
        name = candidates[0] if not domain else ""
    description = meta("og:description", "description", "twitter:description")
    return name, description


def name_from_domain(domain: str) -> str:
    return domain.split(".")[0].replace("-", " ").title()


class SiteRanks:
    """Domains in the Tranco top list, down to `max_rank`, cached on disk."""

    def __init__(self, cache: Path, max_rank: int):
        self.cache = cache
        self.max_rank = max_rank
        self.domains: set[str] | None = None

    def load(self, session: requests.Session | None = None) -> bool:
        if self.domains is not None:
            return True
        fresh = (self.cache.exists() and
                 time.time() - self.cache.stat().st_mtime
                 < TRANCO_MAX_AGE_DAYS * 86400)
        if not fresh:
            self._refresh(session or requests.Session())
        if not self.cache.exists():
            return False
        lines = self.cache.read_text(encoding="utf-8").split()
        self.domains = set(lines[:self.max_rank])
        return True

    def _refresh(self, session: requests.Session) -> None:
        try:
            resp = session.get(TRANCO_URL, timeout=60)
            resp.raise_for_status()
            archive = zipfile.ZipFile(io.BytesIO(resp.content))
            with archive.open(archive.namelist()[0]) as handle:
                rows = io.TextIOWrapper(handle, encoding="utf-8")
                # Ranked order; keep generously more than any sane max_rank.
                top = [line.strip().split(",", 1)[1]
                       for _, line in zip(range(200_000), rows)]
        except Exception as exc:  # a stale cache is still better than none
            print(f"vc: could not refresh the Tranco ranking: "
                  f"{type(exc).__name__}: {exc}")
            return
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        self.cache.write_text("\n".join(top), encoding="utf-8")

    def is_big(self, domain: str) -> bool:
        return bool(self.domains) and domain in self.domains


def pages_for_today(portfolios: list[Portfolio], per_run: int,
                    today: date | None = None) -> list[Portfolio]:
    """A rotating slice, so every fund is read every few nights, not all at once."""
    if not portfolios or per_run <= 0:
        return []
    if per_run >= len(portfolios):
        return list(portfolios)
    start = ((today or date.today()).toordinal() * per_run) % len(portfolios)
    return [portfolios[(start + i) % len(portfolios)] for i in range(per_run)]


def discover(fetcher, settings, db, ranks: SiteRanks, limit: int = 30,
             pages_per_run: int = 8, today: date | None = None
             ) -> tuple[list[Company], VcStats]:
    """Up to `limit` new companies from today's slice of the fund list.

    Funds are drained round-robin, one company each in turn, so one fund with
    a three-hundred-company page does not fill the whole limit.
    """
    stats = VcStats()
    found: list[Company] = []
    if ranks.max_rank and not ranks.load(getattr(fetcher, "session", None)):
        print("vc: no site ranking available, so big companies cannot be told "
              "apart; adding nothing this run")
        return found, stats

    portfolios = load_portfolios(settings.vc_portfolios_path)
    queues: list[tuple[Portfolio, list[tuple[str, str]]]] = []
    for fund in pages_for_today(portfolios, pages_per_run, today):
        html = fetcher.get(fund.url, respect_robots=True)
        if not html:
            stats.pages_failed += 1
            continue
        stats.pages += 1
        entries = sorted(extract_companies(html, fund.url).items())
        stats.candidates += len(entries)
        queues.append((fund, entries))

    seen: set[str] = set()
    while len(found) < limit and any(entries for _, entries in queues):
        for fund, entries in queues:
            if len(found) >= limit or fetcher.budget.exhausted:
                break
            while entries:
                domain, label = entries.pop(0)
                if domain in seen:
                    continue
                seen.add(domain)
                if db.company_exists(domain):
                    stats.already_known += 1
                    continue
                if ranks.is_big(domain):
                    stats.too_big += 1
                    continue
                # One homepage fetch: proves the site is alive and gives the
                # name and description the personal line is written from.
                home = fetcher.get(f"https://{domain}")
                if not home:
                    stats.dead += 1
                    continue
                # A homepage that redirects to another company's domain means
                # it was acquired or renamed; the portfolio entry is stale.
                landed = registrable(urlparse(getattr(fetcher, "last_url", "")
                                              or f"https://{domain}").netloc)
                if landed and landed != domain:
                    stats.moved += 1
                    continue
                site_name, description = site_summary(home, domain)
                found.append(Company(
                    name=(label or site_name or name_from_domain(domain))[:60],
                    domain=domain,
                    website=f"https://{domain}",
                    one_liner=description[:180],
                    description=description[:1200],
                    industry=f"{fund.fund} portfolio",
                    source="vc",
                ))
                stats.added += 1
                break
        if fetcher.budget.exhausted:
            break
    return found, stats
