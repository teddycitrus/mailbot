"""Email validation without paid services.

Three escalating checks, cheapest first:

1. Syntax and disposable/role classification (free, offline).
2. MX lookup, which proves the domain can receive mail at all.
3. SMTP RCPT probe against the domain's MX.

Step 3 is the one that matters and the one with a catch. Google Workspace,
which hosts most startups, answers 250 to every recipient including random
garbage, so a 250 there proves nothing. We detect that per domain by probing a
random address first: if the domain accepts it, the domain is a catch-all and
no address on it can be confirmed. Guessed addresses on catch-all domains are
therefore never treated as verified.
"""

from __future__ import annotations

import re
import smtplib
import uuid
from dataclasses import dataclass
from typing import Optional

try:
    import dns.resolver
except ImportError:  # pragma: no cover - dnspython is in requirements
    dns = None  # type: ignore

from .models import (
    CATCHALL, NO_MX, ROLE_LOCALS, SRC_INFERRED, SRC_SCRAPED, UNDELIVERABLE,
    UNVERIFIED, VERIFIED,
)

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

DISPOSABLE = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com",
    "throwaway.email", "yopmail.com", "trashmail.com",
}

# Never write to these even if scraped; they are not people and some are traps.
NEVER_SEND = {
    "abuse", "postmaster", "noreply", "no-reply", "donotreply", "do-not-reply",
    "security", "privacy", "legal", "dmarc", "spam", "unsubscribe", "bounce",
    "mailer-daemon", "webmaster", "root",
}


@dataclass
class Verdict:
    status: str
    detail: str = ""
    confidence: int = 0

    @property
    def sendable(self) -> bool:
        return self.status in {VERIFIED, CATCHALL}


def valid_syntax(email: str) -> bool:
    return bool(EMAIL_RE.match(email.strip()))


def local_part(email: str) -> str:
    return email.split("@", 1)[0].lower()


def domain_of(email: str) -> str:
    return email.split("@", 1)[1].lower() if "@" in email else ""


def is_never_send(email: str) -> bool:
    return local_part(email) in NEVER_SEND


def is_role_account(email: str) -> bool:
    return local_part(email) in ROLE_LOCALS


# Tokens that mark a mailbox as functional rather than personal, matched as
# substrings because shared inboxes get invented faster than any fixed list can
# track. "carehub" is absent from ROLE_LOCALS but is no more a person than
# "support" is.
NON_NAME_TOKENS = (
    "care", "hub", "team", "info", "hello", "support", "contact", "admin",
    "sales", "job", "hiring", "career", "press", "help", "desk", "mail",
    "inbox", "noreply", "reply", "billing", "legal", "office", "service",
    "account", "partner", "media", "invest", "founder", "recruit", "talent",
    "people", "apply", "join", "connect", "reach", "enquir", "inquir",
)

_GIVEN_NAME_RE = re.compile(r"^[a-z]{3,20}$")


def first_name_from_email(email: str) -> str:
    """Recover a given name from an address like omar@inkeep.com.

    Only answers when the local part really does look like one person's given
    name. Initials, digits and role mailboxes come back empty. The bias is
    deliberately towards returning nothing: the greeting is the first thing the
    recipient reads, so a confidently wrong name there is worse than declining
    to write the message at all.
    """
    if is_role_account(email) or is_never_send(email):
        return ""
    part = local_part(email).split("+", 1)[0]       # austin+hn  -> austin
    part = re.split(r"[._\-]", part, maxsplit=1)[0]          # first.last -> first
    if not _GIVEN_NAME_RE.match(part):              # kk, a1, s -> decline
        return ""
    if any(token in part for token in NON_NAME_TOKENS):
        return ""
    return part.capitalize()


class Verifier:
    """MX and SMTP checks with a per-domain cache to avoid re-probing."""

    # Consecutive connection failures before we accept we are being throttled.
    TIMEOUT_BUDGET = 5

    def __init__(self, db=None, helo_domain: str = "example.com",
                 mail_from: str = "verify@example.com", timeout: int = 8):
        self.db = db
        self.helo_domain = helo_domain
        self.mail_from = mail_from
        self.timeout = timeout
        self._mx: dict[str, Optional[str]] = {}
        self._catchall: dict[str, bool] = {}
        # Mail servers throttle an address that probes too much. Once that
        # starts, further probes only waste minutes and dig the hole deeper, so
        # the run stops probing and falls back to provenance.
        self.timeouts = 0
        self.throttled = False

    # ---------- MX ----------

    def mx_host(self, domain: str) -> Optional[str]:
        domain = domain.lower()
        if domain in self._mx:
            return self._mx[domain]
        if self.db is not None:
            row = self.db.cached_domain(domain)
            if row is not None:
                self._mx[domain] = row["mx_host"] or None
                self._catchall[domain] = bool(row["catchall"])
                return self._mx[domain]
        host = None
        if dns is not None:
            try:
                answers = dns.resolver.resolve(domain, "MX")
                ranked = sorted(
                    (r.preference, str(r.exchange).rstrip(".")) for r in answers
                )
                host = ranked[0][1] if ranked else None
            except Exception:
                host = None
        self._mx[domain] = host
        return host

    # ---------- catch-all detection ----------

    def is_catchall(self, domain: str) -> bool:
        """True when the domain accepts a random address, so probes prove nothing."""
        domain = domain.lower()
        if domain in self._catchall:
            return self._catchall[domain]
        host = self.mx_host(domain)
        if not host:
            self._catchall[domain] = False
            return False
        probe = f"zz{uuid.uuid4().hex[:12]}@{domain}"
        code = self._rcpt(host, [probe]).get(probe, (0, b""))[0]
        result = code == 250
        self._catchall[domain] = result
        if self.db is not None:
            self.db.cache_domain(domain, host, result)
        return result

    # ---------- single-address verification ----------

    def verify(self, email: str, source: str = SRC_SCRAPED) -> Verdict:
        email = email.strip().lower()
        if not valid_syntax(email):
            return Verdict(UNDELIVERABLE, "bad syntax", 0)
        domain = domain_of(email)
        if domain in DISPOSABLE:
            return Verdict(UNDELIVERABLE, "disposable domain", 0)
        if is_never_send(email):
            return Verdict(UNDELIVERABLE, "never-send local part", 0)
        host = self.mx_host(domain)
        if not host:
            return Verdict(NO_MX, "no MX record", 0)
        if self.is_catchall(domain):
            # Cannot confirm anything here. A published address is still fine to
            # use; a guessed one is not, because a wrong guess becomes a bounce.
            if source == SRC_SCRAPED:
                return Verdict(CATCHALL, "catch-all domain, address was published",
                               75 if not is_role_account(email) else 60)
            return Verdict(CATCHALL, "catch-all domain, address was guessed", 25)
        code, msg = self._rcpt(host, [email]).get(email, (0, b""))
        if code == 250:
            return Verdict(VERIFIED, "mailbox accepted",
                           95 if not is_role_account(email) else 80)
        if code in (550, 551, 553, 501, 500):
            return Verdict(UNDELIVERABLE, f"rejected {code}", 0)
        # Inconclusive: the server did not answer, usually because it is
        # throttling us. That says nothing about the mailbox, so fall back to
        # provenance. A human publishing their own address is real evidence; a
        # guess with no answer is still just a guess.
        if source == SRC_SCRAPED:
            return Verdict(UNVERIFIED,
                           f"no SMTP answer ({code}); trusting published source",
                           70 if not is_role_account(email) else 55)
        return Verdict(UNVERIFIED, f"inconclusive {code} {msg[:60]!r}", 40)

    def verify_candidates(self, candidates: list[str], domain: str) -> dict[str, Verdict]:
        """Probe several guesses for one domain over a single SMTP connection."""
        out: dict[str, Verdict] = {}
        host = self.mx_host(domain)
        if not host:
            return {c: Verdict(NO_MX, "no MX record", 0) for c in candidates}
        if self.is_catchall(domain):
            return {
                c: Verdict(CATCHALL, "catch-all domain, address was guessed", 25)
                for c in candidates
            }
        results = self._rcpt(host, candidates)
        for cand in candidates:
            code, msg = results.get(cand, (0, b""))
            if code == 250:
                out[cand] = Verdict(VERIFIED, "mailbox accepted", 90)
            elif code in (550, 551, 553):
                out[cand] = Verdict(UNDELIVERABLE, f"rejected {code}", 0)
            else:
                out[cand] = Verdict(UNVERIFIED, f"inconclusive {code}", 30)
        return out

    def first_verified(self, candidates: list[str], domain: str,
                       chunk: int = 12) -> tuple[str, Verdict] | tuple[None, Verdict]:
        """Probe patterns in order and stop at the first mailbox accepted.

        Short-circuiting matters: it keeps the common case to one or two RCPTs
        instead of a dozen. Probes are chunked across connections because mail
        servers commonly throttle or drop after a handful of recipients on a
        single session.
        """
        host = self.mx_host(domain)
        if not host:
            return None, Verdict(NO_MX, "no MX record", 0)

        # Connection setup dominates the cost of a probe, so the catch-all test
        # rides along on the same session as the candidates instead of opening
        # its own. That roughly halves the SMTP time per company.
        known = self._catchall.get(domain)
        if known is True:
            return None, Verdict(CATCHALL, "catch-all domain, guesses unprovable", 25)
        probe = "" if known is False else f"zz{uuid.uuid4().hex[:12]}@{domain}"

        tried = 0
        answered = 0
        for start in range(0, len(candidates), chunk):
            batch = candidates[start:start + chunk]
            if probe and start == 0:
                batch = [probe] + batch
            results = self._rcpt(host, batch)
            if probe and start == 0:
                code, _ = results.get(probe, (0, b""))
                if code == 250:
                    self._catchall[domain] = True
                    if self.db is not None:
                        self.db.cache_domain(domain, host, True)
                    return None, Verdict(
                        CATCHALL, "catch-all domain, guesses unprovable", 25
                    )
                if code:
                    self._catchall[domain] = False
                    if self.db is not None:
                        self.db.cache_domain(domain, host, False)
                batch = [a for a in batch if a != probe]
            for cand in batch:
                code, msg = results.get(cand, (0, b""))
                tried += 1
                if code:
                    answered += 1
                if code == 250:
                    return cand, Verdict(
                        VERIFIED, f"accepted after {tried} pattern(s)", 90
                    )
        if not answered:
            # The server never actually answered, so we proved nothing. Saying
            # "no pattern accepted" here would be a false negative that quietly
            # discards a reachable founder.
            return None, Verdict(
                UNVERIFIED, f"no SMTP response for any of {tried} pattern(s)", 0
            )
        return None, Verdict(
            UNDELIVERABLE, f"no pattern accepted after {tried} tried", 0
        )

    # ---------- raw SMTP ----------

    def _rcpt(self, host: str, addresses: list[str]) -> dict[str, tuple[int, bytes]]:
        results: dict[str, tuple[int, bytes]] = {}
        if not host:
            return {addr: (0, b"no MX host") for addr in addresses}
        if self.throttled:
            return {addr: (0, b"probing paused, server throttling") for addr in addresses}
        server = None
        try:
            server = smtplib.SMTP(host, 25, timeout=self.timeout)
            server.ehlo(self.helo_domain)
            server.mail(self.mail_from)
            for addr in addresses:
                try:
                    results[addr] = server.rcpt(addr)
                except Exception as exc:
                    results[addr] = (0, str(exc).encode()[:80])
        except Exception as exc:
            if isinstance(exc, (TimeoutError, OSError)):
                self.timeouts += 1
                if self.timeouts >= self.TIMEOUT_BUDGET:
                    self.throttled = True
            for addr in addresses:
                results.setdefault(addr, (0, str(exc).encode()[:80]))
        finally:
            if server is not None:
                try:
                    server.quit()
                except Exception:
                    pass
        return results


def candidate_addresses(first: str, last: str, domain: str) -> list[str]:
    """Every plausible address pattern, ordered by how common it is at startups.

    Order matters because verification short-circuits on the first mailbox the
    server accepts, so the likeliest patterns should be probed first.
    """
    first = re.sub(r"[^a-z]", "", first.lower())
    last = re.sub(r"[^a-z]", "", last.lower())
    if not first or not domain:
        return []
    pats = [first]
    if last:
        pats += [
            f"{first}.{last}",     # john.mannully
            f"{first}{last}",      # johnmannully
            f"{first}{last[0]}",   # johnm
            f"{first[0]}{last}",   # jmannully
            f"{first}_{last}",     # john_mannully
            f"{first}-{last}",     # john-mannully
            f"{first[0]}.{last}",  # j.mannully
            last,                  # mannully
            f"{last}{first[0]}",   # mannullyj
            f"{last}.{first}",     # mannully.john
            f"{first[0]}{last[0]}",  # jm
        ]
    seen, out = set(), []
    for p in pats:
        addr = f"{p}@{domain}"
        if addr not in seen:
            seen.add(addr)
            out.append(addr)
    return out
