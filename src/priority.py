"""Companies named by hand, and the rules that let them jump the queue.

The funnel feeding this bot is built for early-stage startups: fewer than
MAX_EMPLOYEES people, founded inside MAX_COMPANY_AGE_YEARS, found in the YC
feed or a GitHub org search. That is a reasonable default and it is also a
pile of proxies for one thing, "is this place hiring". A list of companies
known to be hiring hard right now is better evidence than any of the proxies,
and most names on such a list fail them: Ramp and Kraken are well past the age
gate, half the list never applied to an accelerator at all.

So a named company skips the gates and sorts ahead of everything else at every
stage after discovery: enriched first, drafted first, sent first.

Nothing else is relaxed. A priority company still needs a contact that
verifies, still obeys the suppression list, the daily cap, the send window and
the one-contact-per-company rule. Being on the list changes what we look at
and in what order, never what we are willing to send.

The file is config/priority.txt, one company per line:

    Ramp = ramp.com      an explicit domain, used as given
    Cognition            resolved when seeded, then written back to the file

Resolution is deliberately timid. Plenty of these names are ordinary English
words, and the .com for "Normal" or "Twenty" or "Instinct" belongs to somebody
with no connection to the company meant. Mailing a stranger is worse than
missing a company, so a guessed domain is only accepted when the site serving
it says the company's name back. Everything else is reported and left for a
human to fill in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

# Tried in this order. Anything a startup in this list plausibly sits on.
TLDS = ("com", "ai", "io", "co", "dev", "sh", "xyz", "so", "tech")

# Dropped from the end of a name when generating a second stem: "Pangram Labs"
# is as likely to be pangram.com as pangramlabs.com.
SUFFIXES = ("labs", "lab", "inc", "co", "corp", "software", "research",
            "systems", "technologies", "technology", "robotics", "ai",
            "bio", "nuclear", "aerospace", "space", "industries")

# Below this, a name is too short or too common for a homepage mentioning it
# to be evidence of anything. "Re", "fal" and "Rox" need an explicit domain.
MIN_EVIDENCE_CHARS = 5

_SQUASH = re.compile(r"[^a-z0-9]+")


def squash(text: str) -> str:
    """Lowercase, letters and digits only. "Brain Co." -> "brainco"."""
    return _SQUASH.sub("", (text or "").lower())


def normalise(name: str) -> str:
    """The key a company is matched on, here and against discovered rows."""
    return squash(name)


@dataclass
class Entry:
    name: str
    domain: str = ""
    note: str = ""

    @property
    def key(self) -> str:
        return normalise(self.name)


def parse(text: str) -> list[Entry]:
    """Read the priority file. Blank lines and # comments are ignored."""
    out: list[Entry] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        name, _, domain = line.partition("=")
        entry = Entry(name.strip(), domain.strip().lower())
        if not entry.name or entry.key in seen:
            continue
        seen.add(entry.key)
        out.append(entry)
    return out


def load(path: str | Path) -> list[Entry]:
    file = Path(path)
    if not file.exists():
        return []
    return parse(file.read_text(encoding="utf-8"))


def render(entries: Iterable[Entry], header: str = "") -> str:
    """The file's text, with resolved domains filled in.

    Column-aligned so the unresolved ones are obvious at a glance.
    """
    items = list(entries)
    width = max((len(e.name) for e in items), default=0)
    lines = [header.rstrip("\n")] if header else []
    for entry in items:
        if entry.domain:
            lines.append(f"{entry.name.ljust(width)} = {entry.domain}")
        else:
            lines.append(f"{entry.name.ljust(width)} ="
                         + (f"   # {entry.note}" if entry.note else ""))
    return "\n".join(lines) + "\n"


def stems(name: str) -> list[str]:
    """Domain stems worth trying for a name, best first."""
    squashed = squash(name)
    if not squashed:
        return []
    out = [squashed]
    words = [w for w in _SQUASH.sub(" ", name.lower()).split() if w]
    if len(words) > 1:
        hyphenated = "-".join(words)
        if hyphenated not in out:
            out.append(hyphenated)
        if words[-1] in SUFFIXES:
            trimmed = "".join(words[:-1])
            if trimmed and trimmed not in out:
                out.append(trimmed)
    return out


def candidates(name: str) -> list[str]:
    """Every domain worth checking for a name, best first."""
    out: list[str] = []
    for stem in stems(name):
        for tld in TLDS:
            candidate = f"{stem}.{tld}"
            if candidate not in out:
                out.append(candidate)
    return out


def site_confirms(html: str, name: str) -> bool:
    """Does the site at a guessed domain say this company's name back?

    The whole safety of guessing rests on this. A page that never mentions the
    name is somebody else's page, whatever its MX records say.
    """
    key = squash(name)
    if not html or len(key) < MIN_EVIDENCE_CHARS:
        return False
    # The head of the document carries the title, the meta description and the
    # nav. A mention further down is likelier to be a coincidence.
    return key in squash(html[:20000])


def resolve(name: str, fetcher, verifier) -> tuple[str, str]:
    """Find the domain for a name, or explain why it could not be found.

    Cheap check first: a domain with no MX cannot receive mail, so it is not
    the one we want and is rejected on a DNS lookup rather than a page fetch.
    Only survivors of that cost an HTTP request.
    """
    key = squash(name)
    if len(key) < MIN_EVIDENCE_CHARS:
        return "", "name too short to guess safely, set the domain by hand"
    tried = 0
    for domain in candidates(name):
        if not verifier.mx_host(domain):
            continue
        tried += 1
        html = fetcher.get(f"https://{domain}") or ""
        if site_confirms(html, name):
            return domain, ""
        if tried >= 6:
            break
    if tried:
        return "", f"{tried} domain(s) take mail but none names the company"
    return "", "no candidate domain takes mail"


class PrioritySet:
    """The names on the list, for asking "is this one of them?" quickly."""

    def __init__(self, entries: Iterable[Entry] = ()):
        items = list(entries)
        self.names = {e.key for e in items}
        self.domains = {e.domain.lower() for e in items if e.domain}

    def __bool__(self) -> bool:
        return bool(self.names)

    def __len__(self) -> int:
        return len(self.names)

    def has(self, name: str = "", domain: str = "") -> bool:
        return (bool(name) and normalise(name) in self.names) or (
            bool(domain) and domain.lower() in self.domains)


def from_settings(settings) -> PrioritySet:
    path: Optional[Path] = getattr(settings, "priority_path", None)
    return PrioritySet(load(path) if path else ())
