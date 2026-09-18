"""GitHub as a free enrichment source.

Git commits carry the author's email because the author put it there. That
makes them fundamentally different from a guessed address: nothing is being
inferred, so they stay usable on catch-all domains where SMTP verification can
prove nothing.

There is a second, larger win. Seeing enes@, furkan@ and hadi@ on one domain
reveals that the domain uses the `first` pattern, which lets us construct a
founder's address with real evidence behind it rather than a blind guess.

Unauthenticated the API allows 60 requests an hour, which is enough for a
daily run. Set GITHUB_TOKEN for 5000.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field

from .patterns import apply_pattern, detect as local_pattern, name_parts
from .ratelimit import Budget, RateLimiter
from .verifier import is_never_send

API = "https://api.github.com"
ORG_RE = re.compile(r"github\.com/([A-Za-z0-9][A-Za-z0-9\-_.]{0,38})/?", re.I)

# Paths that look like an org link but are not one.
NOT_ORGS = {
    "features", "pricing", "about", "login", "join", "explore", "topics",
    "collections", "trending", "events", "sponsors", "readme", "orgs", "apps",
    "marketplace", "security", "enterprise", "customer-stories", "site",
}


@dataclass
class GitHubFindings:
    emails: dict[str, str] = field(default_factory=dict)  # email -> author name
    pattern: str = ""
    org: str = ""
    calls: int = 0


def extract_org(html: str, exclude: set[str] = frozenset()) -> str:
    """Pick the most frequently linked GitHub org on a page."""
    counts: Counter[str] = Counter()
    for match in ORG_RE.finditer(html or ""):
        slug = match.group(1)
        if slug.lower() in NOT_ORGS or slug.lower() in exclude:
            continue
        counts[slug] += 1
    return counts.most_common(1)[0][0] if counts else ""


class GitHubClient:
    def __init__(self, token: str = "", max_calls: int = 50,
                 per_minute: int = 20, timeout: int = 20):
        self.token = token
        self.budget = Budget(max_calls, "github")
        self.limiter = RateLimiter(per_minute)
        self.timeout = timeout
        self.exhausted = False

    def _get(self, path: str):
        if self.exhausted or not self.budget.take():
            return None
        self.limiter.acquire()
        headers = {
            "User-Agent": "mailbot/0.1",
            "Accept": "application/vnd.github+json",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(API + path, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429):
                # Hourly quota gone. Stop asking rather than burning the budget.
                self.exhausted = True
            return None
        except Exception:
            return None

    def org_exists(self, slug: str) -> bool:
        return bool(self._get(f"/orgs/{slug}"))

    def search_orgs(self, query: str, per_page: int = 100, page: int = 1) -> list[dict]:
        """One page of the org search. Returns logins only; see `get_user`.

        The search endpoint has its own quota, 30 requests a minute when
        authenticated, which the client's 20-a-minute limiter stays under.
        """
        payload = self._get(
            "/search/users?q=" + urllib.parse.quote(query)
            + f"&per_page={per_page}&page={page}"
        )
        return (payload or {}).get("items", []) or []

    def get_user(self, login: str) -> dict:
        """Full profile for one account. Search hits do not carry `blog`."""
        return self._get(f"/users/{login}") or {}

    def harvest(self, org: str, domain: str, repos: int = 3,
                commits: int = 40) -> GitHubFindings:
        """Collect on-domain commit emails and infer the domain's pattern."""
        found = GitHubFindings(org=org)
        repo_list = self._get(f"/orgs/{org}/repos?sort=pushed&per_page={repos}")
        if not repo_list:
            repo_list = self._get(f"/users/{org}/repos?sort=pushed&per_page={repos}")
        if not repo_list:
            return found

        domain = domain.lower()
        patterns: Counter[str] = Counter()
        for repo in repo_list[:repos]:
            name = repo.get("name")
            if not name:
                continue
            payload = self._get(f"/repos/{org}/{name}/commits?per_page={commits}")
            if not payload:
                continue
            for entry in payload:
                author = (entry.get("commit") or {}).get("author") or {}
                email = (author.get("email") or "").lower().strip()
                who = author.get("name") or ""
                if not email or is_never_send(email):
                    continue
                if not email.endswith("@" + domain):
                    continue
                found.emails.setdefault(email, who)
                guess = local_pattern(email, who, domain.split(".")[0])
                if guess:
                    patterns[guess] += 1
        if patterns:
            found.pattern = patterns.most_common(1)[0][0]
        found.calls = self.budget.used
        return found


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def find_org_for(fetcher, website: str, html: str = "",
                 company_name: str = "", client=None) -> str:
    """Find the company's GitHub org.

    Prefers a link in HTML we have already fetched, since company homepages are
    often JS rendered and give up nothing. Falls back to trying the company
    name as an org slug, which is right surprisingly often and costs one call.
    """
    org = extract_org(html, exclude={"ycombinator"})
    if org:
        return org
    if not html and website:
        org = extract_org(fetcher.get(website) or "", exclude={"ycombinator"})
        if org:
            return org
    slug = slugify(company_name)
    if slug and client is not None and not client.exhausted:
        if client.org_exists(slug):
            return slug
    return ""
