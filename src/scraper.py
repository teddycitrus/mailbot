"""Polite HTTP fetching and the two parsers the pipeline needs.

Everything here is free: the YC public company page for founder names, roles
and bios, and the company's own site for any address it chooses to publish.
Requests are rate limited and capped, identify themselves honestly in the User
-Agent, and respect robots.txt for the company sites we crawl.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from .ratelimit import Budget, RateLimiter

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
IMAGE_SUFFIX = (".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif", ".ico")

# Pages most likely to carry a human address, cheapest guesses first.
CONTACT_PATHS = ("", "/about", "/team", "/contact", "/company", "/about-us", "/careers")

ROLE_HINTS = (
    "founder", "co-founder", "cofounder", "ceo", "cto", "chief", "head of",
    "lead", "principal", "director", "vp ", "engineer",
)


@dataclass
class Person:
    first_name: str = ""
    last_name: str = ""
    role: str = ""
    bio: str = ""
    linkedin: str = ""

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p)


@dataclass
class SiteScrape:
    emails: set[str] = field(default_factory=set)
    pages_fetched: int = 0


def split_name(full: str) -> tuple[str, str]:
    parts = [p for p in re.split(r"\s+", full.strip()) if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


class Fetcher:
    """Shared HTTP client with a token bucket and a hard per-run budget."""

    def __init__(self, user_agent: str, per_minute: int = 30,
                 max_fetches: int = 400, timeout: int = 10):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})
        self.limiter = RateLimiter(per_minute)
        self.budget = Budget(max_fetches, "http")
        self.timeout = timeout
        self._robots: dict[str, RobotFileParser | None] = {}

    def get(self, url: str, respect_robots: bool = True) -> str | None:
        if self.budget.exhausted:
            return None
        if respect_robots and not self.allowed(url):
            return None
        if not self.budget.take():
            return None
        self.limiter.acquire()
        try:
            resp = self.session.get(url, timeout=self.timeout, allow_redirects=True)
        except Exception:
            return None
        if resp.status_code != 200:
            return None
        ctype = resp.headers.get("Content-Type", "")
        if "html" not in ctype and "text" not in ctype:
            return None
        return resp.text

    def get_json(self, url: str):
        if not self.budget.take():
            return None
        self.limiter.acquire()
        try:
            resp = self.session.get(url, timeout=max(self.timeout, 30))
            if resp.status_code != 200:
                return None
            return resp.json()
        except Exception:
            return None

    def allowed(self, url: str) -> bool:
        """Honour robots.txt. On any doubt we fetch, on an explicit deny we skip."""
        try:
            parts = urlparse(url)
            root = f"{parts.scheme}://{parts.netloc}"
        except Exception:
            return False
        if root not in self._robots:
            parser = RobotFileParser()
            parser.set_url(urljoin(root, "/robots.txt"))
            try:
                parser.read()
            except Exception:
                parser = None
            self._robots[root] = parser
        parser = self._robots[root]
        if parser is None:
            return True
        try:
            return parser.can_fetch(self.session.headers["User-Agent"], url)
        except Exception:
            return True


def parse_yc_founders(html: str) -> list[Person]:
    """Pull founder name, role and bio off a YC company page.

    Primary path keys off each founder's LinkedIn anchor, whose enclosing block
    reads 'Name | Role | Bio'. Falls back to the Active Founders text section
    for founders who have no LinkedIn link.
    """
    soup = BeautifulSoup(html, "lxml")
    people: dict[str, Person] = {}

    for anchor in soup.select('a[href*="linkedin.com/in"]'):
        href = anchor.get("href", "")
        block = anchor.find_parent("div")
        for _ in range(3):
            if block is not None and len(block.get_text(strip=True)) > 40:
                break
            block = block.find_parent("div") if block is not None else None
        if block is None:
            continue
        fields = [f.strip() for f in block.get_text(" | ", strip=True).split(" | ") if f.strip()]
        if not fields:
            continue
        name = fields[0]
        if not _looks_like_name(name):
            continue
        role = fields[1] if len(fields) > 1 else ""
        bio = fields[2] if len(fields) > 2 else ""
        first, last = split_name(name)
        existing = people.get(name)
        if existing is None or (len(bio) > len(existing.bio)):
            people[name] = Person(first, last, role, bio, href)

    if not people:
        people.update({p.full_name: p for p in _fallback_founders(soup)})
    return list(people.values())


def _looks_like_name(text: str) -> bool:
    if not (2 <= len(text) <= 60):
        return False
    words = text.split()
    if not (1 <= len(words) <= 4):
        return False
    return all(w[:1].isupper() for w in words if w[:1].isalpha())


def _fallback_founders(soup: BeautifulSoup) -> list[Person]:
    text = soup.get_text("\n")
    idx = text.lower().find("active founders")
    if idx < 0:
        return []
    lines = [ln.strip() for ln in text[idx:idx + 2000].split("\n") if ln.strip()]
    people: list[Person] = []
    seen: set[str] = set()
    for i, line in enumerate(lines[1:], start=1):
        if _looks_like_name(line) and line.lower() not in seen:
            role = lines[i + 1] if i + 1 < len(lines) else ""
            if not any(h in role.lower() for h in ROLE_HINTS):
                continue
            seen.add(line.lower())
            first, last = split_name(line)
            bio = lines[i + 2] if i + 2 < len(lines) else ""
            people.append(Person(first, last, role, bio))
    return people


def extract_emails(html: str, domain: str) -> set[str]:
    """On-domain addresses only. Third-party addresses are noise or trackers."""
    found: set[str] = set()
    soup = BeautifulSoup(html, "lxml")
    for anchor in soup.select('a[href^="mailto:"]'):
        raw = anchor.get("href", "")[7:].split("?")[0].strip().rstrip(").,;")
        if raw:
            found.add(raw.lower())
    for match in EMAIL_RE.findall(html):
        found.add(match.lower().rstrip(").,;"))
    domain = domain.lower()
    return {
        e for e in found
        if not e.endswith(IMAGE_SUFFIX)
        and "@" in e
        and e.split("@", 1)[1] == domain
    }


def scrape_site(fetcher: Fetcher, website: str, domain: str,
                paths: tuple[str, ...] = CONTACT_PATHS) -> SiteScrape:
    """Walk a handful of likely pages looking for published addresses."""
    result = SiteScrape()
    if not website:
        return result
    if not website.startswith("http"):
        website = "https://" + website
    for path in paths:
        url = urljoin(website, path) if path else website
        html = fetcher.get(url)
        if html is None:
            continue
        result.pages_fetched += 1
        result.emails |= extract_emails(html, domain)
        if result.emails and path:
            break
    return result
