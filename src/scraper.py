"""Polite HTTP fetching and the two parsers the pipeline needs.

Everything here is free: the YC public company page for founder names, roles
and bios, and the company's own site for any address it chooses to publish.
Requests are rate limited and capped, identify themselves honestly in the User
-Agent, and respect robots.txt for the company sites we crawl.
"""

from __future__ import annotations

import json
import re
from html import unescape as html_unescape
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from .ratelimit import Budget, RateLimiter

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Punctuation inside an embedded JSON blob arrives escaped. A site that
# ships its content in a __NEXT_DATA__ payload writes ">" as a >
# sequence, so a mailto written that way matched the address pattern
# starting at the "u003e" and produced u003elegal@acme.ai: a local part
# that does not exist, scored like any other published address, and
# bound for a bounce.
JSON_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")
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
    # Founders the site names itself, read only when the caller asks.
    people: list[Person] = field(default_factory=list)


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
        self.last_url = ""

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
        # Where redirects ended, for callers that care a site has moved.
        self.last_url = resp.url
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
            self._robots[root] = self._read_robots(root)
        parser = self._robots[root]
        if parser is None:
            return True
        try:
            return parser.can_fetch(self.session.headers["User-Agent"], url)
        except Exception:
            return True

    def _read_robots(self, root: str) -> RobotFileParser | None:
        """robots.txt fetched as this client, with RobotFileParser's status rules.

        RobotFileParser.read() fetches with urllib's own User-Agent, which
        bot firewalls answer with 403, and a 403 makes it deny the whole site.
        A quarter of company sites were skipped that way while their actual
        robots.txt allowed everyone.
        """
        parser = RobotFileParser()
        parser.set_url(urljoin(root, "/robots.txt"))
        try:
            resp = self.session.get(parser.url, timeout=self.timeout)
        except Exception:
            return None
        if resp.status_code in (401, 403):
            parser.disallow_all = True
        elif 400 <= resp.status_code < 500:
            parser.allow_all = True
        elif resp.status_code == 200:
            parser.parse(resp.text.splitlines())
        else:
            return None
        return parser


def parse_yc_founders(html: str) -> list[Person]:
    """Pull founder name, role and bio off a YC company page.

    Primary path reads the page's embedded data-page JSON. Since late 2026 YC
    renders founders client-side, so the static HTML has no founder text or
    LinkedIn anchors at all; the two markup paths below are kept for older
    copies of the page. They key off each founder's LinkedIn anchor, whose
    enclosing block reads 'Name | Role | Bio', then the Active Founders text.
    """
    soup = BeautifulSoup(html, "lxml")
    embedded = _embedded_founders(soup)
    if embedded:
        return embedded
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


def _embedded_founders(soup: BeautifulSoup) -> list[Person]:
    """Founders from the page's data-page JSON, active ones only."""
    for el in soup.find_all(attrs={"data-page": True}):
        try:
            data = json.loads(el["data-page"])
            founders = data["props"]["company"]["founders"]
        except (ValueError, KeyError, TypeError):
            continue
        people: list[Person] = []
        for f in founders if isinstance(founders, list) else []:
            if not isinstance(f, dict) or f.get("is_active") is False:
                continue
            name = " ".join(str(f.get("full_name") or "").split())
            if not _looks_like_name(name):
                continue
            first, last = split_name(name)
            people.append(Person(
                first, last,
                role=str(f.get("title") or "").strip(),
                bio=" ".join(str(f.get("founder_bio") or "").split()),
                linkedin=str(f.get("linkedin_url") or "").strip(),
            ))
        return people
    return []


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


def decode_escapes(text: str) -> str:
    """Undo JSON and HTML escaping so an address is readable before matching.

    Decoding first is the whole point. Running the pattern over the raw
    source lets an escape sequence become part of the local part, and the
    result looks like a perfectly ordinary address right up to the bounce.
    """
    text = JSON_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text)
    return html_unescape(text)


def extract_emails(html: str, domain: str) -> set[str]:
    """On-domain addresses only. Third-party addresses are noise or trackers."""
    found: set[str] = set()
    soup = BeautifulSoup(html, "lxml")
    for anchor in soup.select('a[href^="mailto:"]'):
        raw = anchor.get("href", "")[7:].split("?")[0].strip().rstrip(").,;")
        if raw:
            found.add(raw.lower())
    for match in EMAIL_RE.findall(decode_escapes(html)):
        found.add(match.lower().rstrip(").,;"))
    domain = domain.lower()
    return {
        e for e in found
        if not e.endswith(IMAGE_SUFFIX)
        and "@" in e
        and e.split("@", 1)[1] == domain
    }


def scrape_site(fetcher: Fetcher, website: str, domain: str,
                paths: tuple[str, ...] = CONTACT_PATHS,
                want_people: bool = False) -> SiteScrape:
    """Walk a handful of likely pages looking for published addresses.

    With want_people the walk also reads founder names off the same pages,
    and keeps going past the first address until it has found some, since
    the about and team pages that name founders often come after contact.
    """
    result = SiteScrape()
    if not website:
        return result
    if not website.startswith("http"):
        website = "https://" + website
    seen: set[str] = set()
    for path in paths:
        url = urljoin(website, path) if path else website
        html = fetcher.get(url)
        if html is None:
            continue
        result.pages_fetched += 1
        result.emails |= extract_emails(html, domain)
        if want_people:
            for person in parse_site_founders(html):
                if person.full_name.lower() not in seen:
                    seen.add(person.full_name.lower())
                    result.people.append(person)
        if result.emails and path and (result.people or not want_people):
            break
    return result


# A founder's title, as a team page writes it. Deliberately narrow: "Head of
# Growth" is a person, but not one this campaign writes to off a guess.
FOUNDER_TITLE = re.compile(
    r"\b(co-?\s?founder|founder|ceo|cto|chief executive|chief technology)\b", re.I)
# Headings and nav labels that look like a capitalised name.
NOT_NAME_WORDS = {
    "our", "the", "team", "meet", "about", "us", "founder", "founders",
    "co-founder", "cofounder", "ceo", "cto", "and", "with", "of", "for",
    "company", "contact", "careers", "join", "leadership", "story", "mission",
    "people", "advisors", "investors", "board", "news", "blog", "home",
}


def _site_name(text: str) -> bool:
    if not _looks_like_name(text):
        return False
    words = text.split()
    if not (2 <= len(words) <= 3):
        return False
    if any(w.lower().strip(",.") in NOT_NAME_WORDS for w in words):
        return False
    return all(re.fullmatch(r"[A-Z][a-zA-Z'\-]+\.?", w) for w in words)


def parse_site_founders(html: str) -> list[Person]:
    """Founders a company names on its own site.

    Two signals, strongest first. Schema.org JSON-LD, where an Organization
    lists its founder or a Person carries a jobTitle. Then team-page text: a
    name line followed by a founder title, or "Name, Co-founder & CEO" on one
    line. Only founder-level titles count; see FOUNDER_TITLE.
    """
    soup = BeautifulSoup(html, "lxml")
    people: dict[str, Person] = {}

    def add(name: str, role: str) -> None:
        name = " ".join(name.split())
        if _site_name(name) and name not in people:
            first, last = split_name(name)
            people[name] = Person(first, last, role.strip()[:80])

    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
                continue
            if not isinstance(item, dict):
                continue
            stack.extend(v for v in item.values() if isinstance(v, (dict, list)))
            founders = item.get("founder") or item.get("founders")
            for f in founders if isinstance(founders, list) else [founders]:
                if isinstance(f, dict) and f.get("name"):
                    add(str(f["name"]), "Founder")
            if item.get("@type") == "Person" and item.get("name"):
                title = str(item.get("jobTitle") or "")
                if FOUNDER_TITLE.search(title):
                    add(str(item["name"]), title)

    lines = [ln.strip() for ln in soup.get_text("\n").split("\n") if ln.strip()]
    for i, line in enumerate(lines):
        if len(line) > 80:
            continue
        inline = re.match(r"^([A-Z][^,|\-]{2,40}?)\s*[,|\-]\s*(.+)$", line)
        if inline and FOUNDER_TITLE.search(inline.group(2)):
            add(inline.group(1), inline.group(2))
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if len(nxt) <= 60 and FOUNDER_TITLE.search(nxt):
            add(line, nxt)
    return list(people.values())
