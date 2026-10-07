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

Step 3 needs outbound port 25, which the server's cloud provider blocks. So
the probe can go through a relay instead: an SSH tunnel the laptop holds open
to the server (scripts/probe_relay.ps1), which the server sees as a SOCKS5
proxy on a loopback port. Whenever the laptop is on, probes leave from the
laptop's own connection; whenever it is off, the relay refuses and probing
pauses instead of recording failures as findings. SMTP_PROBE_RELAY turns it on.
"""

from __future__ import annotations

import re
import smtplib
import socket
import uuid
from dataclasses import dataclass
from typing import Optional

try:
    import dns.resolver
except ImportError:  # pragma: no cover - dnspython is in requirements
    dns = None  # type: ignore

from .models import (
    CATCHALL, NO_MX, ROLE_LOCALS, SRC_SCRAPED, UNDELIVERABLE, UNVERIFIED,
    VERIFIED,
)

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

DISPOSABLE = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com",
    "throwaway.email", "yopmail.com", "trashmail.com",
}

# Never write to these even if scraped; they are not people and some are traps.
NEVER_SEND = {
    "abuse", "postmaster", "security", "privacy", "legal", "dmarc", "spam",
    "webmaster", "root",
}

# Matched as substrings of the local part instead of whole mailbox names. A
# mailbox that says in its own name that nobody reads it is dead however the
# name is dressed up: noreply-jobs@, jobs.no-reply@, bounces+7a1f@.
#
# "support" is here for a different reason: it reaches a real person, just
# never the right one. A ticket queue answers questions about the product, so
# an internship pitch landing in it is ignored at best, and at worst marked as
# spam by someone whose whole job is clearing that queue.
NEVER_SEND_TOKENS = (
    "noreply", "donotreply", "mailerdaemon", "unsubscribe", "bounce",
    "support",
)

# The same idea applied to subdomains, since @users.noreply.github.com and
# @reply.acme.com route to a robot no matter what the local part says. Only
# labels above the registrable domain are checked, so a company that is really
# called reply.io stays reachable.
NEVER_SEND_HOSTS = ("noreply", "donotreply", "reply", "bounce", "mailer")

# Organisations that turn up in hiring threads and company pages but are not
# the startup itself: the accelerator and its job board. Mail there reaches
# someone who cannot hire John and who talks to every founder he might write to.
NEVER_SEND_DOMAINS = {"ycombinator.com", "workatastartup.com", "news.ycombinator.com"}


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


def _squash(text: str) -> str:
    """Reduce to letters and digits so no-reply, no_reply and NoReply agree."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def is_never_send(email: str) -> bool:
    local = _squash(local_part(email))
    if local in NEVER_SEND:
        return True
    if any(token in local for token in NEVER_SEND_TOKENS):
        return True
    if ".".join(domain_of(email).split(".")[-2:]) in NEVER_SEND_DOMAINS:
        return True
    subdomains = _squash("".join(domain_of(email).split(".")[:-2]))
    return any(token in subdomains for token in NEVER_SEND_HOSTS)


def is_role_account(email: str) -> bool:
    return local_part(email) in ROLE_LOCALS


def is_personal_mailbox(email: str) -> bool:
    """True when the address belongs to one person rather than a queue.

    Shared inboxes (hello@, info@, founders@) answered 1 in 35 cold emails
    against roughly 1 in 15 for a named founder, and the ones that do not
    answer are the likeliest to mark the message as spam, which costs every
    later send. They are refused outright rather than ranked last.
    """
    return not (is_role_account(email) or is_never_send(email))


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


# ---------- probe relay ----------

Relay = tuple[str, int]

# Any mail exchanger will do for a reachability test; the question is whether
# port 25 opens at all, not whether one host is up.
REACHABILITY_HOST = "gmail-smtp-in.l.google.com"


class RelayDown(OSError):
    """The relay itself is not there, so no probe through it can run.

    Distinct from a probe that failed, which may be one dead mail host. A
    refused or silent relay means the laptop is off or asleep, and every probe
    after this one would fail the same way.
    """


def parse_relay(value: str) -> Optional[Relay]:
    """ "127.0.0.1:1080" -> ("127.0.0.1", 1080). Empty means probe directly."""
    value = (value or "").strip()
    if not value:
        return None
    host, sep, port = value.rpartition(":")
    if not sep or not host or not port.isdigit():
        raise ValueError(f"SMTP_PROBE_RELAY must be host:port, got {value!r}")
    return host, int(port)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise OSError("relay closed the connection")
        data += chunk
    return data


def socks5_connect(relay: Relay, host: str, port: int,
                   timeout: float) -> socket.socket:
    """Open host:port through a SOCKS5 relay, no authentication.

    The hostname is passed through unresolved, so the laptop resolves it.
    Anything that goes wrong before the relay has answered its greeting is
    RelayDown: the greeting does not depend on the target, so a relay that
    cannot manage it cannot manage anything.
    """
    try:
        sock = socket.create_connection(relay, timeout=timeout)
    except OSError as exc:
        raise RelayDown(f"relay {relay[0]}:{relay[1]} refused ({exc})") from exc
    try:
        try:
            sock.sendall(b"\x05\x01\x00")
            greeting = _recv_exact(sock, 2)
        except OSError as exc:
            # A sleeping laptop leaves the server's end of the tunnel open
            # until keepalives notice, so this is a timeout, not a refusal.
            raise RelayDown(f"relay did not answer ({exc})") from exc
        if greeting != b"\x05\x00":
            raise RelayDown(f"relay spoke something other than SOCKS5 {greeting!r}")
        name = host.encode("idna")
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(name)]) + name
                     + port.to_bytes(2, "big"))
        head = _recv_exact(sock, 4)
        if head[1] != 0:
            raise OSError(f"relay could not reach {host}:{port} (SOCKS code {head[1]})")
        size = {1: 4, 4: 16}.get(head[3])
        if size is None:
            size = _recv_exact(sock, 1)[0]
        _recv_exact(sock, size + 2)   # the bound address, which we do not need
        return sock
    except BaseException:
        sock.close()
        raise


class _RelayedSMTP(smtplib.SMTP):
    """smtplib, with the TCP connection opened through the relay."""

    def __init__(self, relay: Relay, *args, **kwargs):
        self._relay = relay          # set first: SMTP.__init__ connects
        super().__init__(*args, **kwargs)

    def _get_socket(self, host, port, timeout):
        return socks5_connect(self._relay, host, port, timeout)


def port25_reachable(relay: Optional[Relay] = None, host: str = REACHABILITY_HOST,
                     timeout: float = 10) -> tuple[bool, str]:
    """Whether a probe could run right now, and what happened if not.

    Opens port 25 on a real mail host, directly or through the relay, and
    reads the banner. Cheap enough to call before every batch of probes.
    """
    try:
        if relay:
            sock = socks5_connect(relay, host, 25, timeout)
        else:
            sock = socket.create_connection((host, 25), timeout=timeout)
        with sock:
            sock.settimeout(timeout)
            banner = sock.recv(120).decode("utf-8", "replace").strip()
    except RelayDown as exc:
        return False, f"relay offline: {exc}"
    except Exception as exc:
        return False, f"cannot reach {host}:25 ({type(exc).__name__}: {exc})"
    if not banner.startswith("220"):
        return False, f"{host} said {banner[:40]!r}"
    return True, f"{host} reachable" + (" through the relay" if relay else "")


class Verifier:
    """MX and SMTP checks with a per-domain cache to avoid re-probing."""

    # Connection failures *in a row* before we accept we are being throttled.
    # In a row, not in total: a run that probes ninety companies meets a
    # handful of dead hosts in the ordinary course of things, and counting
    # those against one lifetime budget silently turned verification off
    # partway through every long run. The reset lives in _rcpt.
    TIMEOUT_BUDGET = 5

    def __init__(self, db=None, helo_domain: str = "example.com",
                 mail_from: str = "verify@example.com", timeout: int = 8,
                 relay: Optional[Relay] = None):
        self.db = db
        self.helo_domain = helo_domain
        self.mail_from = mail_from
        self.timeout = timeout
        self.relay = relay
        self._mx: dict[str, Optional[str]] = {}
        self._catchall: dict[str, bool] = {}
        # Mail servers throttle an address that probes too much. Once that
        # starts, further probes only waste minutes and dig the hole deeper, so
        # the run stops probing and falls back to provenance.
        self.timeouts = 0
        self.throttled = False

    # ---------- MX ----------

    def mx_lookup(self, domain: str) -> tuple[Optional[str], bool]:
        """The domain's mail host, and whether the resolver actually answered.

        "Takes no mail" and "the lookup never came back" are the same None to
        mx_host, and on 2026-09-22 that cost ninety companies: the morning's
        DNS was down, every resolve raised, and each company was parked as
        "domain has no MX record" -- a finding, deliberately absent from
        RETRYABLE_SKIPS, written from a lookup that had proved nothing.

        So report the two apart, and never memoise a failure: one bad minute
        would otherwise stand in for the domain for the rest of the run.
        """
        domain = domain.lower()
        if domain in self._mx:
            return self._mx[domain], True
        if self.db is not None:
            row = self.db.cached_domain(domain)
            if row is not None:
                self._mx[domain] = row["mx_host"] or None
                self._catchall[domain] = bool(row["catchall"])
                return self._mx[domain], True
        if dns is None:
            return None, False
        try:
            answers = dns.resolver.resolve(domain, "MX")
            ranked = sorted(
                (r.preference, str(r.exchange).rstrip(".")) for r in answers
            )
            host = ranked[0][1] if ranked else None
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            # The domain answered for itself: it does not exist, or it exists
            # and publishes no mail host. Both are findings.
            host = None
        except Exception:
            # SERVFAIL, timeout, no nameserver reachable, no network at all.
            # Nothing was learned, so write nothing down.
            return None, False
        self._mx[domain] = host
        return host, True

    def mx_host(self, domain: str) -> Optional[str]:
        """Just the host, for callers that cannot act on the difference."""
        return self.mx_lookup(domain)[0]

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
        if not code:
            # The server never answered, so we learned nothing. Recording
            # "not a catch-all" here would be a guess written down as fact,
            # and mx_host reloads it on every later run, so the guess would
            # outlive the outage that caused it. Answer for this call only.
            return False
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
            if self.relay:
                server = _RelayedSMTP(self.relay, host, 25, timeout=self.timeout)
            else:
                server = smtplib.SMTP(host, 25, timeout=self.timeout)
            server.ehlo(self.helo_domain)
            code, msg = server.mail(self.mail_from)
            if code != 250:
                # The server refused us, not a mailbox: a blocklisted network,
                # or an IPv6 address with no reverse DNS. Any RCPT answer after
                # this is about our IP, and a 550 read as "no such user" would
                # suppress a real person for good.
                raise OSError(f"sender refused {code} {msg[:40]!r}")
            # A completed handshake proves we are not being throttled, so the
            # failure streak starts again from zero. Without this the budget
            # was a lifetime allowance rather than a streak, and five dead
            # hosts scattered across a long run stopped all further probing.
            self.timeouts = 0
            for addr in addresses:
                try:
                    results[addr] = server.rcpt(addr)
                except Exception as exc:
                    results[addr] = (0, str(exc).encode()[:80])
        except Exception as exc:
            if isinstance(exc, RelayDown):
                # No streak needed: the laptop is off, and waiting out four
                # more timeouts would only park four more companies wrongly.
                self.throttled = True
            elif isinstance(exc, (TimeoutError, OSError)):
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
