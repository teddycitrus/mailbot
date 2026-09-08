"""Address-pattern learning: the piece that makes guessing stop being guessing.

Commercial enrichment tools do three things here. They map the naming
convention of a domain from addresses they already trust, they compute which
conventions dominate overall, and they use both to synthesise candidates in
descending order of probability. All three are free to do; the expensive part
of those products is the proprietary corpus, and this module grows its own from
every address the pipeline confirms.

The payoff is largest on catch-all domains. SMTP verification proves nothing
there, so an unaided guess is unusable. But if the team's own published
addresses show the domain uses `first`, then applying `first` to a known
founder is applied evidence rather than a coin flip.

One caveat kept deliberately visible: candidates are probed in order and the
first acceptance wins, so the observed distribution is biased toward whichever
pattern is tried first. `global_prior` therefore only reorders, and never
prunes, the candidate set.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone

# Every convention understood, in the order used when nothing has been learned.
# Roughly descending frequency at small startups.
PATTERN_NAMES: tuple[str, ...] = (
    "first", "first.last", "firstlast", "firstl", "flast", "first_last",
    "first-last", "f.last", "last", "lastf", "last.first", "fl",
)


def name_parts(name: str, domain_label: str = "") -> list[str]:
    """Split a person's name into tokens.

    Handles real names and the login-style names that git commits carry, such
    as 'shagun-singh-inkeep', by splitting on any non-letter and dropping a
    token that is just the company.
    """
    parts = [p for p in re.split(r"[^A-Za-z]+", (name or "").lower()) if p]
    label = (domain_label or "").lower()
    if label and len(parts) > 1:
        parts = [p for p in parts if p != label] or parts
    return parts


def build(pattern: str, first: str, last: str) -> str:
    """Render one local-part for a name under a named convention."""
    first = re.sub(r"[^a-z0-9]", "", (first or "").lower())
    last = re.sub(r"[^a-z0-9]", "", (last or "").lower())
    if not first:
        return ""
    table = {
        "first": first,
        "first.last": f"{first}.{last}" if last else "",
        "firstlast": f"{first}{last}" if last else "",
        "firstl": f"{first}{last[0]}" if last else "",
        "flast": f"{first[0]}{last}" if last else "",
        "first_last": f"{first}_{last}" if last else "",
        "first-last": f"{first}-{last}" if last else "",
        "f.last": f"{first[0]}.{last}" if last else "",
        "last": last,
        "lastf": f"{last}{first[0]}" if last else "",
        "last.first": f"{last}.{first}" if last else "",
        "fl": f"{first[0]}{last[0]}" if last else "",
    }
    return table.get(pattern, "")


def apply_pattern(pattern: str, first: str, last: str, domain: str) -> str:
    local = build(pattern, first, last)
    return f"{local}@{domain}" if local else ""


def detect(email: str, name: str, domain_label: str = "") -> str:
    """Which convention produced this address, if any."""
    local = email.split("@", 1)[0].lower()
    parts = name_parts(name, domain_label)
    if not parts:
        return ""
    first, last = parts[0], (parts[-1] if len(parts) > 1 else "")
    for pattern in PATTERN_NAMES:
        candidate = build(pattern, first, last)
        if candidate and candidate == local:
            return pattern
    return ""


# --------------------------------------------------------------- persistence

def learn(db, email: str, first: str, last: str, source: str = "") -> str:
    """Record the convention an address reveals about its domain."""
    if db is None or "@" not in email:
        return ""
    domain = email.split("@", 1)[1].lower()
    pattern = detect(email, f"{first} {last}".strip(), domain.split(".")[0])
    if not pattern:
        return ""
    row = db.conn.execute(
        "SELECT pattern, evidence FROM domain_patterns WHERE domain = ?", (domain,)
    ).fetchone()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if row is None:
        db.conn.execute(
            """INSERT INTO domain_patterns (domain, pattern, evidence, source, updated_at)
               VALUES (?,?,1,?,?)""", (domain, pattern, source, now))
    elif row["pattern"] == pattern:
        db.conn.execute(
            "UPDATE domain_patterns SET evidence = evidence + 1, updated_at = ? "
            "WHERE domain = ?", (now, domain))
    else:
        # Domains do run more than one convention. Keep the better supported
        # one rather than letting the newest observation win.
        if row["evidence"] <= 1:
            db.conn.execute(
                "UPDATE domain_patterns SET pattern = ?, evidence = 1, source = ?, "
                "updated_at = ? WHERE domain = ?", (pattern, source, now, domain))
    db.conn.commit()
    return pattern


def known_pattern(db, domain: str) -> tuple[str, int]:
    if db is None:
        return "", 0
    row = db.conn.execute(
        "SELECT pattern, evidence FROM domain_patterns WHERE domain = ?",
        (domain.lower(),)).fetchone()
    return (row["pattern"], int(row["evidence"])) if row else ("", 0)


def global_prior(db) -> list[str]:
    """Patterns ordered by how often they have been observed, commonest first.

    Only reorders the candidate list. Anything unobserved keeps its default
    position rather than being dropped, because the corpus is small and biased
    by probe order.
    """
    if db is None:
        return list(PATTERN_NAMES)
    counts = Counter()
    for row in db.conn.execute(
            "SELECT pattern, evidence FROM domain_patterns"):
        counts[row["pattern"]] += int(row["evidence"])
    if not counts:
        return list(PATTERN_NAMES)
    seen = [p for p, _ in counts.most_common() if p in PATTERN_NAMES]
    return seen + [p for p in PATTERN_NAMES if p not in seen]


def ordered_candidates(db, first: str, last: str, domain: str) -> list[str]:
    """Candidate addresses, most probable first.

    A convention already observed on this exact domain outranks everything;
    after that the corpus-wide ordering applies.
    """
    domain = domain.lower()
    order: list[str] = []
    pattern, _evidence = known_pattern(db, domain)
    if pattern:
        order.append(pattern)
    for name in global_prior(db):
        if name not in order:
            order.append(name)

    out, seen = [], set()
    for name in order:
        local = build(name, first, last)
        if not local:
            continue
        address = f"{local}@{domain}"
        if address not in seen:
            seen.add(address)
            out.append(address)
    return out
