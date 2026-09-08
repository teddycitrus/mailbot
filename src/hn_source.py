"""Hacker News "Who is hiring" as a second discovery source.

The monthly thread is the highest-quality free contact source available: the
person doing the hiring writes the post and usually puts their own address in
it. That makes those addresses published rather than inferred, so they stay
usable on catch-all domains where SMTP verification proves nothing.

Honest limitation. A YC record carries batch year and team size, so the
under-five-years and under-200-people filters can be applied properly. An HN
comment carries neither. Companies from here are therefore filtered on location
and on being an ML/AI role, and stored with source='hn' so they remain
distinguishable from the properly filtered YC set.

Uses the public Algolia HN API: no key, no quota worth worrying about.
"""

from __future__ import annotations

import html as html_mod
import re
from dataclasses import dataclass, field

import requests

SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
ITEM_URL = "https://hn.algolia.com/api/v1/items/{item_id}"

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
URL_RE = re.compile(r"https?://([A-Za-z0-9.\-]+)")
TAG_RE = re.compile(r"<[^>]+>")

ML_TERMS = (
    "machine learning", " ml ", "ml/", "deep learning", "neural", "llm",
    "artificial intelligence", " ai ", "ai/", "ai-", "genai", "nlp",
    "computer vision", "pytorch", "tensorflow", "transformer", "rag",
    "embedding", "inference", "model training", "data scientist",
)

# Free-mail hosts: a personal address, not a company one.
CONSUMER = {
    "gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "protonmail.com", "proton.me", "aol.com", "fastmail.com",
    "me.com", "live.com", "msn.com", "gmx.com", "mail.com", "zoho.com",
}


@dataclass
class HNPost:
    company: str = ""
    location: str = ""
    emails: list[str] = field(default_factory=list)
    domain: str = ""
    website: str = ""
    text: str = ""
    is_ml: bool = False


def strip_html(text: str) -> str:
    return html_mod.unescape(TAG_RE.sub(" ", text or ""))


def looks_ml(text: str) -> bool:
    low = f" {text.lower()} "
    return any(term in low for term in ML_TERMS)


def find_location(text: str, targets: tuple[str, ...]) -> str:
    low = text.lower()
    for target in targets:
        if target and target in low:
            return target.title()
    return ""


def parse_comment(text: str, targets: tuple[str, ...]) -> HNPost:
    """Pull company, location and contact address out of one hiring comment.

    The convention in these threads is a pipe-delimited header line:
    'Company | Role | Location | REMOTE | full-time'.
    """
    clean = strip_html(text)
    post = HNPost(text=clean, is_ml=looks_ml(clean))

    header = next((l for l in clean.splitlines() if l.strip()), "")
    name = header.split("|", 1)[0] if "|" in header else header
    # Headers often trail a URL or parenthetical straight after the name.
    name = re.split(r"\s*[(\[]|https?://|\s{2,}", name)[0]
    post.company = name.strip(" *-•,:").strip()[:60]

    post.location = find_location(clean, targets)

    seen: list[str] = []
    for address in EMAIL_RE.findall(clean):
        address = address.lower().rstrip(").,;")
        if address not in seen:
            seen.append(address)
    post.emails = seen

    # Prefer a company domain taken from an address; fall back to a linked host.
    for address in seen:
        domain = address.split("@", 1)[1]
        if domain not in CONSUMER:
            post.domain = domain
            break
    if not post.domain:
        for host in URL_RE.findall(clean):
            host = host.lower().replace("www.", "")
            if host and "news.ycombinator" not in host:
                post.domain = host
                break
    if post.domain:
        post.website = f"https://{post.domain}"
    return post


def latest_thread_id(session: requests.Session | None = None,
                     timeout: int = 20) -> str:
    """Newest 'Ask HN: Who is hiring?' story id."""
    get = (session or requests).get
    try:
        resp = get(SEARCH_URL, params={
            "query": "Ask HN: Who is hiring?", "tags": "story", "hitsPerPage": 5,
        }, timeout=timeout)
        if resp.status_code != 200:
            return ""
        for hit in resp.json().get("hits", []):
            title = (hit.get("title") or "").lower()
            if "who is hiring" in title and "ask hn" in title:
                return str(hit["objectID"])
    except Exception:
        return ""
    return ""


def fetch_posts(targets: tuple[str, ...], thread_id: str = "",
                timeout: int = 40) -> list[HNPost]:
    """Every qualifying comment in the newest hiring thread."""
    thread_id = thread_id or latest_thread_id()
    if not thread_id:
        return []
    try:
        resp = requests.get(ITEM_URL.format(item_id=thread_id), timeout=timeout)
        if resp.status_code != 200:
            return []
        children = resp.json().get("children", []) or []
    except Exception:
        return []

    posts = []
    for child in children:
        if not child.get("text"):
            continue
        post = parse_comment(child["text"], targets)
        if not (post.is_ml and post.location and post.emails and post.domain):
            continue
        posts.append(post)
    return posts
