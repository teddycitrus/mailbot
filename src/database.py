"""SQLite persistence and the duplicate-send guarantees.

The hard rule this module enforces: an address is emailed at most once, ever.
That is backed by a UNIQUE index on contacts.email plus an explicit check
against the sends log and the suppression list before any send is allowed.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

from .models import (
    BOUNCED, CATCHALL, Company, Contact, ENRICHED, GUESS_PROVENANCE, MAIN_LANE,
    NO_CONTACTS, PENDING, PROV_GUESS_RETRY, QUEUED, REPLIED, SENT,
    SKIP_CATCHALL_UNPROVABLE, SKIP_MX_UNRESOLVED,
    SKIP_PROBE_UNAVAILABLE, SUPPRESSED, VERIFIED, lane_deliverable,
)
from .verifier import is_personal_mailbox, sendable_address_class

# A named person first, everyone else after. Sorting on priority alone let a
# verified hello@ tie with a founder's own address and fill half of each day.
NAMED_FIRST = "(COALESCE(c.first_name, '') <> '') DESC"

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS companies (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    domain        TEXT NOT NULL,
    website       TEXT DEFAULT '',
    location      TEXT DEFAULT '',
    industry      TEXT DEFAULT '',
    one_liner     TEXT DEFAULT '',
    description   TEXT DEFAULT '',
    batch         TEXT DEFAULT '',
    founded_year  INTEGER,
    employees     INTEGER,
    is_hiring     INTEGER DEFAULT 0,
    yc_url        TEXT DEFAULT '',
    github_org    TEXT DEFAULT '',
    -- Named in config/priority.txt. Sorts ahead of everything else at every
    -- stage after discovery, and exempt from the size and age gates. See
    -- priority.py.
    priority      INTEGER DEFAULT 0,
    source        TEXT DEFAULT 'yc',
    status        TEXT DEFAULT 'new',
    skip_reason   TEXT DEFAULT '',
    created_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_domain ON companies(domain);

CREATE TABLE IF NOT EXISTS contacts (
    id            INTEGER PRIMARY KEY,
    company_id    INTEGER REFERENCES companies(id) ON DELETE CASCADE,
    email         TEXT NOT NULL,
    first_name    TEXT DEFAULT '',
    last_name     TEXT DEFAULT '',
    role          TEXT DEFAULT '',
    bio           TEXT DEFAULT '',
    linkedin      TEXT DEFAULT '',
    confidence    INTEGER DEFAULT 0,
    source        TEXT DEFAULT 'scraped',
    verify_status TEXT DEFAULT 'unverified',
    verify_detail TEXT DEFAULT '',
    status        TEXT DEFAULT 'pending',
    created_at    TEXT NOT NULL,
    -- How the address was found; decides whether a catch-all one may be
    -- sent to. See models.TRUSTED_PROVENANCE.
    provenance    TEXT DEFAULT '',
    -- The only sending lane allowed to mail this person, '' for any. See
    -- models.lane_deliverable.
    lane          TEXT DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_email ON contacts(email);
CREATE INDEX IF NOT EXISTS idx_contacts_status ON contacts(status);

CREATE TABLE IF NOT EXISTS drafts (
    id          INTEGER PRIMARY KEY,
    contact_id  INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    subject     TEXT NOT NULL,
    body        TEXT NOT NULL,
    attachments TEXT DEFAULT '',
    status      TEXT DEFAULT 'queued',
    -- Message-ID of this draft's copy in the Gmail Drafts folder, empty when
    -- there is no copy. It is how the copy is found again and deleted once
    -- the message has gone out, so that it cannot be sent a second time by
    -- hand. See mirror.py.
    gmail_message_id TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_drafts_contact ON drafts(contact_id);

CREATE TABLE IF NOT EXISTS sends (
    id          INTEGER PRIMARY KEY,
    contact_id  INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    email       TEXT NOT NULL,
    subject     TEXT DEFAULT '',
    message_id  TEXT DEFAULT '',
    backend     TEXT DEFAULT '',
    status      TEXT DEFAULT 'sent',
    error       TEXT DEFAULT '',
    sent_at     TEXT NOT NULL,
    local_date  TEXT NOT NULL,
    -- The sending account it left from.
    lane        TEXT DEFAULT 'main'
);
CREATE INDEX IF NOT EXISTS idx_sends_email ON sends(email);
CREATE INDEX IF NOT EXISTS idx_sends_date ON sends(local_date);

CREATE TABLE IF NOT EXISTS suppression (
    email      TEXT PRIMARY KEY,
    reason     TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS domain_cache (
    domain     TEXT PRIMARY KEY,
    mx_host    TEXT DEFAULT '',
    catchall   INTEGER DEFAULT 0,
    checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS domain_patterns (
    domain     TEXT PRIMARY KEY,
    pattern    TEXT NOT NULL,
    evidence   INTEGER DEFAULT 1,
    source     TEXT DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company_snapshots (
    domain    TEXT NOT NULL,
    team_size INTEGER,
    seen_on   TEXT NOT NULL,
    PRIMARY KEY (domain, seen_on)
);

-- Each lane's cap for a day, as its send path computed it. The volume lane
-- reads main's to know when main is full and the open queue is overflow.
CREATE TABLE IF NOT EXISTS daily_caps (
    lane       TEXT NOT NULL,
    local_date TEXT NOT NULL,
    cap        INTEGER NOT NULL,
    PRIMARY KEY (lane, local_date)
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL,
    ref        TEXT DEFAULT '',
    detail     TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _undeliverable_reason(row, lane: str = MAIN_LANE) -> str:
    reserved = row["lane"] if "lane" in row.keys() else ""
    if reserved and reserved != lane:
        return f"reserved for the {reserved} lane"
    if row["verify_status"] == CATCHALL:
        return f"catch-all, untrusted source ({row['provenance'] or 'unknown'})"
    return f"not SMTP-verified ({row['verify_status']})"


def _reserved(row) -> str:
    return (row["lane"] if "lane" in row.keys() else "") or ""


class Database:
    # Scheduled jobs overlap by design: drafting runs every two hours, sending
    # every fifteen minutes, and both carry a wake trigger that fires them at
    # the same instant. WAL keeps readers out of the way, but two writers still
    # collide, and the stdlib default of five seconds is short enough that a
    # run could die on a lock held by a sibling doing IMAP work.
    BUSY_TIMEOUT_SECONDS = 30

    def __init__(self, path: str | Path, lane: str = MAIN_LANE):
        self.path = Path(path)
        # The sending account this connection acts for. Sends are recorded
        # under it, and the per-day accounting the ramp and the bounce brake
        # read (counts, active days, bounce rate) only sees its own sends.
        # Every gate on who may be mailed stays global.
        self.lane = lane or MAIN_LANE
        self.conn = sqlite3.connect(self.path, timeout=self.BUSY_TIMEOUT_SECONDS)
        self.conn.execute(
            f"PRAGMA busy_timeout = {self.BUSY_TIMEOUT_SECONDS * 1000}")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---------- companies ----------

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created.

        CREATE TABLE IF NOT EXISTS silently leaves an existing table alone, so a
        new column has to be added explicitly or every read of it raises on the
        databases that matter, the ones already carrying live send history.
        """
        for table, column, ddl in (
            ("companies", "github_org", "TEXT DEFAULT ''"),
            ("companies", "priority", "INTEGER DEFAULT 0"),
            ("drafts", "gmail_message_id", "TEXT DEFAULT ''"),
            ("contacts", "provenance", "TEXT DEFAULT ''"),
            # Which sending account a message left from. Every send before
            # lanes existed was the main account's, which the default says.
            ("sends", "lane", f"TEXT DEFAULT '{MAIN_LANE}'"),
            # The only lane allowed to mail this contact, "" for any.
            ("contacts", "lane", "TEXT DEFAULT ''"),
        ):
            have = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"
                )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sends_lane_date ON sends(lane, local_date)")
        self.conn.commit()

    def upsert_company(self, c: Company) -> int:
        """Insert, or return the existing id. Domain is the identity key, and
        a YC page already on file is too: a company that changed domain kept
        its YC page, and adding it again under the new domain wrote to its
        founders twice."""
        row = self.conn.execute(
            "SELECT id FROM companies WHERE domain = ?", (c.domain,)
        ).fetchone()
        if row is None and c.yc_url:
            row = self.conn.execute(
                "SELECT id FROM companies WHERE yc_url = ? ORDER BY id LIMIT 1",
                (c.yc_url,)).fetchone()
        if row:
            return int(row["id"])
        cur = self.conn.execute(
            """INSERT INTO companies
               (name, domain, website, location, industry, one_liner, description,
                batch, founded_year, employees, is_hiring, yc_url, github_org,
                priority, source, status, skip_reason, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (c.name, c.domain, c.website, c.location, c.industry, c.one_liner,
             c.description, c.batch, c.founded_year, c.employees, int(c.is_hiring),
             c.yc_url, c.github_org, int(getattr(c, "priority", 0)), c.source,
             c.status, c.skip_reason, _now()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def company_exists(self, domain: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM companies WHERE domain = ?", (domain.lower(),)
        ).fetchone() is not None

    def set_company_status(self, company_id: int, status: str, reason: str = "") -> None:
        self.conn.execute(
            "UPDATE companies SET status = ?, skip_reason = ? WHERE id = ?",
            (status, reason, company_id),
        )
        self.conn.commit()

    def companies_by_status(self, status: str, limit: int = 100) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM companies WHERE status = ?
               ORDER BY priority DESC, is_hiring DESC, id LIMIT ?""",
            (status, limit),
        ).fetchall()

    def mark_priority(self, company_id: int, priority: int = 1) -> None:
        self.conn.execute(
            "UPDATE companies SET priority = ? WHERE id = ?", (priority, company_id)
        )
        self.conn.commit()

    def clear_priorities(self) -> int:
        """Reset every flag, so a name dropped from the file loses its place."""
        cur = self.conn.execute(
            "UPDATE companies SET priority = 0 WHERE priority != 0")
        self.conn.commit()
        return cur.rowcount

    def all_companies(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, name, domain, priority FROM companies").fetchall()

    def get_company(self, company_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM companies WHERE id = ?", (company_id,)
        ).fetchone()

    def record_snapshot(self, domain: str, team_size: Optional[int]) -> None:
        """Log today's headcount so growth becomes measurable over time.

        Headcount climbing is the best free proxy for a company that has raised
        and is spending. One row per domain per day; re-running is harmless.
        """
        self.conn.execute(
            "INSERT OR REPLACE INTO company_snapshots (domain, team_size, seen_on) "
            "VALUES (?,?,?)",
            (domain.lower(), team_size, _now()[:10]),
        )
        self.conn.commit()

    def headcount_growth(self, domain: str) -> Optional[int]:
        """Change in team size between the first and latest sighting."""
        rows = self.conn.execute(
            "SELECT team_size FROM company_snapshots WHERE domain = ? "
            "AND team_size IS NOT NULL ORDER BY seen_on", (domain.lower(),)
        ).fetchall()
        if len(rows) < 2:
            return None
        return int(rows[-1]["team_size"]) - int(rows[0]["team_size"])

    # ---------- contacts ----------

    def upsert_contact(self, c: Contact) -> Optional[int]:
        """Insert a contact. Returns None if the email is already known."""
        email = c.email.lower().strip()
        if self.contact_exists(email):
            return None
        cur = self.conn.execute(
            """INSERT INTO contacts
               (company_id, email, first_name, last_name, role, bio, linkedin,
                confidence, source, verify_status, verify_detail, status, created_at,
                provenance, lane)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (c.company_id, email, c.first_name, c.last_name, c.role, c.bio,
             c.linkedin, c.confidence, c.source, c.verify_status, c.verify_detail,
             c.status, _now(), c.provenance, getattr(c, "lane", "") or ""),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def contact_exists(self, email: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM contacts WHERE email = ?", (email.lower(),)
        ).fetchone() is not None

    def set_contact_status(self, contact_id: int, status: str) -> None:
        self.conn.execute(
            "UPDATE contacts SET status = ? WHERE id = ?", (status, contact_id)
        )
        self.conn.commit()

    def update_verification(self, contact_id: int, status: str, detail: str,
                            confidence: int) -> None:
        self.conn.execute(
            """UPDATE contacts SET verify_status = ?, verify_detail = ?,
               confidence = ? WHERE id = ?""",
            (status, detail, confidence, contact_id),
        )
        self.conn.commit()

    def contacts_by_status(self, status: str, limit: int = 100) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT c.*, co.name AS company_name, co.domain AS company_domain,
                      co.one_liner, co.description, co.location, co.batch, co.website
               FROM contacts c LEFT JOIN companies co ON co.id = c.company_id
               WHERE c.status = ?
               ORDER BY """ + NAMED_FIRST + """,
                        COALESCE(co.priority, 0) DESC, c.confidence DESC, c.id
               LIMIT ?""",
            (status, limit),
        ).fetchall()

    def company_family(self, company_id: int) -> list[int]:
        """This company and any other row for it: same YC page, other domain.

        Older databases hold 17 such pairs, added before upsert_company knew
        to look. Every per-company rule reads the family, so a reply, an
        opt-out, the daily spacing and the per-company cap all span both.
        """
        rows = self.conn.execute(
            """SELECT o.id FROM companies c JOIN companies o
                 ON o.id = c.id OR (c.yc_url <> '' AND o.yc_url = c.yc_url)
               WHERE c.id = ? ORDER BY o.id""", (company_id,)).fetchall()
        return [int(r[0]) for r in rows] or [company_id]

    def company_key(self, company_id: int) -> int:
        """One id per real company: the oldest row in its family."""
        return self.company_family(company_id)[0]

    def _family_marks(self, company_id: int) -> tuple[str, list[int]]:
        family = self.company_family(company_id)
        return ",".join("?" for _ in family), family

    def same_person_elsewhere(self, email: str) -> str:
        """Another address for this contact's person, already in play, or "".

        The same full name at another row of the same company is one person
        written to twice. The earlier row wins: a later one is refused while
        the earlier is queued, sent, answered or closed.
        """
        row = self.conn.execute(
            "SELECT id, company_id, first_name, last_name FROM contacts WHERE email = ?",
            (email.lower().strip(),)).fetchone()
        if (row is None or row["company_id"] is None
                or not (row["first_name"] or "").strip()
                or not (row["last_name"] or "").strip()):
            return ""
        marks, family = self._family_marks(row["company_id"])
        if len(family) < 2:
            return ""
        other = self.conn.execute(
            f"""SELECT email FROM contacts
                WHERE company_id IN ({marks}) AND id <> ?
                  AND lower(first_name) = lower(?) AND lower(last_name) = lower(?)
                  AND (status IN (?,?,?,?) OR (status IN (?,?) AND id < ?))
                ORDER BY id LIMIT 1""",
            (*family, row["id"], row["first_name"], row["last_name"],
             SENT, REPLIED, BOUNCED, SUPPRESSED, PENDING, QUEUED, row["id"]),
        ).fetchone()
        return other["email"] if other else ""

    def company_contacted_count(self, company_id: int, provenance: str = "") -> int:
        """People at this company already written to or waiting in the queue.

        For a guess retry, a guess that bounced is not counted: the retry
        takes that person's place rather than adding a person.
        """
        skip_failed = provenance == PROV_GUESS_RETRY
        marks = ",".join("?" for _ in GUESS_PROVENANCE)
        fam_marks, family = self._family_marks(company_id)
        return int(self.conn.execute(
            f"""SELECT COUNT(*) FROM contacts
               WHERE company_id IN ({fam_marks}) AND status IN (?,?,?,?)"""
            + (f" AND NOT (status = ? AND provenance IN ({marks})"
               " AND verify_status = 'catchall' AND lane = 'volume')" if skip_failed else ""),
            (*family, SENT, REPLIED, BOUNCED, QUEUED)
            + ((BOUNCED, *GUESS_PROVENANCE) if skip_failed else ()),
        ).fetchone()[0])

    def company_closed(self, company_id: int, provenance: str = "") -> str:
        """Why nobody else at this company should be written to, or "".

        A reply means the conversation is happening; writing to a colleague
        after that reads as going around them. An opt-out or a hard bounce
        closes it too: the first is a no for the company, the second usually
        means the address pattern we used for everyone there is wrong.

        Except for a guess retry, asked about with its `provenance`: a guess
        that hard-bounced disproved one convention at the company, which is
        exactly what the retry corrects, so that bounce does not close the
        company to it. A reply or an opt-out still does.
        """
        fam_marks, family = self._family_marks(company_id)
        rows = self.conn.execute(
            f"""SELECT c.status, c.provenance, c.verify_status, c.lane, s.reason
               FROM contacts c
               LEFT JOIN suppression s ON s.email = c.email
               WHERE c.company_id IN ({fam_marks})
                 AND (c.status = ? OR s.reason IN ('opt-out reply', 'hard bounce'))""",
            (*family, REPLIED),
        ).fetchall()
        if any(row["status"] == REPLIED for row in rows):
            return "someone there already replied"
        for row in rows:
            # Only the volume lane's own catch-all guesses: see _VOLUME_GUESS.
            if (provenance == PROV_GUESS_RETRY and row["reason"] == "hard bounce"
                    and row["provenance"] in GUESS_PROVENANCE
                    and row["verify_status"] == CATCHALL and row["lane"] == "volume"):
                continue
            return f"a colleague's {row['reason']}"
        return ""

    def company_slot(self, company_id: int, cap: int,
                     provenance: str = "") -> tuple[bool, str]:
        """Whether one more person at this company may be queued."""
        closed = self.company_closed(company_id, provenance)
        if closed:
            return False, closed
        have = self.company_contacted_count(company_id, provenance)
        if have >= cap:
            return False, f"company already has {have} contacted"
        return True, ""

    # The volume lane's unprovable guesses: catch-all, reserved for that lane.
    # Main's own guesses carry provenance "guess" too, but those passed an
    # SMTP probe on a domain that rejects strangers; a bounce there says the
    # mailbox died, not that the convention is wrong, and a retry would be a
    # probe-able address wrongly filed as a catch-all.
    _VOLUME_GUESS = ("c.verify_status = 'catchall' AND c.lane = 'volume' "
                     "AND c.provenance IN ({marks})")

    def guess_bounces(self, before: str) -> list[sqlite3.Row]:
        """Volume-lane guesses that hard-bounced at or before `before` (UTC
        ISO), oldest first: the ones whose convention is now disproved."""
        marks = ",".join("?" for _ in GUESS_PROVENANCE)
        return self.conn.execute(
            f"""SELECT c.*, s.created_at AS bounced_at FROM contacts c
                JOIN suppression s ON s.email = c.email
                WHERE c.status = ? AND s.reason = 'hard bounce'
                  AND {self._VOLUME_GUESS.format(marks=marks)}
                  AND c.company_id IS NOT NULL AND s.created_at <= ?
                ORDER BY s.created_at""",
            (BOUNCED, *GUESS_PROVENANCE, before),
        ).fetchall()

    def guesses_at(self, company_id: int) -> list[sqlite3.Row]:
        """Every volume-lane guess and retry made at one company, oldest first."""
        marks = ",".join("?" for _ in GUESS_PROVENANCE)
        return self.conn.execute(
            f"""SELECT c.*, co.domain AS company_domain FROM contacts c
                JOIN companies co ON co.id = c.company_id
                WHERE c.company_id = ? AND {self._VOLUME_GUESS.format(marks=marks)}
                ORDER BY c.id""",
            (company_id, *GUESS_PROVENANCE),
        ).fetchall()

    def companies_sent_on(self, local_date: str) -> set[int]:
        """Companies that got a first email on this date, as company_key ids,
        so two rows of one company count as the same company."""
        return {self.company_key(r[0]) for r in self.conn.execute(
            """SELECT DISTINCT c.company_id FROM sends s
               JOIN contacts c ON c.id = s.contact_id
               WHERE s.local_date = ? AND s.status IN ('sent', 'sending')
                 AND c.company_id IS NOT NULL""",
            (local_date,),
        )}

    # ---------- the duplicate-send gate ----------

    def can_send_to(self, email: str, lane: str = "") -> tuple[bool, str]:
        """Single source of truth for whether an address may receive mail.

        Judged for this connection's lane, or for `lane` when given: drafting
        runs on main but renders the volume lane's reserved contacts too, and
        has to ask whether that lane could send them.
        """
        lane = lane or self.lane
        email = email.lower().strip()
        if not is_personal_mailbox(email):
            return False, "shared or blocked inbox"
        if self.is_suppressed(email):
            return False, "suppressed"
        row = self.conn.execute(
            "SELECT status, verify_status, provenance, lane FROM contacts WHERE email = ?",
            (email,)
        ).fetchone()
        if row and row["status"] in {SENT, REPLIED, BOUNCED, SUPPRESSED}:
            return False, f"already {row['status']}"
        # Any lane's send counts: a person is written to once, by one account.
        prior = self.conn.execute(
            "SELECT 1 FROM sends WHERE email = ? AND status IN ('sent', 'sending')",
            (email,)
        ).fetchone()
        if prior:
            return False, "already in send log"
        twin = self.same_person_elsewhere(email)
        if twin:
            return False, f"same person as {twin}"
        # A stored contact is mailed only once its own mailbox accepted an SMTP
        # RCPT, or, on a catch-all domain, when it came from a trusted source.
        # An address with no row yet is being vetted by enrich, which applies
        # the same rule before storing it (see models.deliverable). The volume
        # lane adds only its own reserved guess tier to that.
        if row and not lane_deliverable(row["verify_status"], row["provenance"],
                                        _reserved(row), lane):
            return False, _undeliverable_reason(row, lane)
        return True, ""

    def can_send_to_again(self, email: str) -> tuple[bool, str]:
        """The follow-up gate: like can_send_to, but a prior send is expected.

        Everything that means "leave this person alone" still blocks. Only the
        "already contacted once" rule is relaxed, and only for someone whose
        status is still exactly SENT.
        """
        email = email.lower().strip()
        if not is_personal_mailbox(email):
            return False, "shared or blocked inbox"
        if self.is_suppressed(email):
            return False, "suppressed"
        row = self.conn.execute(
            "SELECT status, verify_status, provenance, lane FROM contacts WHERE email = ?",
            (email,)
        ).fetchone()
        if row is None:
            return False, "unknown contact"
        if row["status"] in {REPLIED, BOUNCED, SUPPRESSED}:
            return False, f"already {row['status']}"
        if row["status"] != SENT:
            return False, f"status is {row['status']}, not sent"
        if not lane_deliverable(row["verify_status"], row["provenance"],
                                _reserved(row), self.lane):
            return False, _undeliverable_reason(row, self.lane)
        # The bump goes from the account the first message came from, so it
        # threads under it and reads as the same person writing again.
        first = self.first_send_lane(email)
        if first and first != self.lane:
            return False, f"first mailed from the {first} lane"
        return True, ""

    def first_send_lane(self, email: str) -> str:
        row = self.conn.execute(
            "SELECT lane FROM sends WHERE email = ? AND status IN ('sent', 'sending') "
            "ORDER BY id LIMIT 1", (email.lower().strip(),)).fetchone()
        return (row["lane"] or MAIN_LANE) if row else ""

    def is_suppressed(self, email: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM suppression WHERE email = ?", (email.lower(),)
        ).fetchone() is not None

    def suppress(self, email: str, reason: str = "") -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO suppression (email, reason, created_at) VALUES (?,?,?)",
            (email.lower().strip(), reason, _now()),
        )
        self.conn.execute(
            "UPDATE contacts SET status = ? WHERE email = ?", (SUPPRESSED, email.lower())
        )
        self.conn.commit()

    # ---------- drafts ----------

    def queue_draft(self, contact_id: int, subject: str, body: str,
                    attachments: Iterable[str] = ()) -> int:
        cur = self.conn.execute(
            """INSERT INTO drafts (contact_id, subject, body, attachments, status, created_at)
               VALUES (?,?,?,?,'queued',?)
               ON CONFLICT(contact_id) DO UPDATE SET
                 subject=excluded.subject, body=excluded.body,
                 attachments=excluded.attachments, status='queued'""",
            (contact_id, subject, body, "|".join(attachments), _now()),
        )
        self.conn.execute(
            "UPDATE contacts SET status = ? WHERE id = ?", (QUEUED, contact_id)
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def queued_drafts(self, limit: int | None = 100) -> list[sqlite3.Row]:
        """Drafts this lane may send: open ones, and any reserved for it.

        A draft reserved for another lane is left out here rather than
        refused at send time, where a refusal marks it skipped for good.
        """
        return self.conn.execute(
            """SELECT d.*, c.email, c.first_name, c.last_name, c.role, c.company_id,
                      c.verify_status, c.provenance, c.lane AS contact_lane,
                      co.name AS company_name, co.location AS company_location
               FROM drafts d
               JOIN contacts c ON c.id = d.contact_id
               LEFT JOIN companies co ON co.id = c.company_id
               WHERE d.status = 'queued' AND c.status = ?
                 AND COALESCE(c.lane, '') IN ('', ?)
               ORDER BY """ + NAMED_FIRST + """,
                        COALESCE(co.priority, 0) DESC, c.confidence DESC, d.id
               LIMIT ?""",
            (QUEUED, self.lane, -1 if limit is None else limit),
        ).fetchall()

    def mark_draft(self, draft_id: int, status: str) -> None:
        self.conn.execute("UPDATE drafts SET status = ? WHERE id = ?", (status, draft_id))
        self.conn.commit()

    def queued_draft_count(self, reserved: bool | None = None) -> int:
        """How deep the queue is, for the top-up target in queue().

        `reserved` counts only drafts held for one lane (True) or only open
        ones (False), so guesses waiting for the volume lane never fill the
        target that keeps the main queue topped up. None counts both.
        """
        clause = ("" if reserved is None
                  else " AND COALESCE(c.lane, '') <> ''" if reserved
                  else " AND COALESCE(c.lane, '') = ''")
        return int(self.conn.execute(
            """SELECT COUNT(*) FROM drafts d JOIN contacts c ON c.id = d.contact_id
               WHERE d.status = 'queued' AND c.status = ?""" + clause,
            (QUEUED,),
        ).fetchone()[0])

    # ---------- Gmail Drafts mirror ----------

    def drafts_to_mirror(self, limit: int = 50) -> list[sqlite3.Row]:
        """Queued drafts that have no copy in the Gmail Drafts folder yet.

        Only ones any lane may send. A draft reserved for the volume lane is
        one the main account must never send, so a copy in its Drafts, one
        tap from going out by hand, is exactly what has to be avoided.
        """
        return self.conn.execute(
            """SELECT d.*, c.email, c.first_name, c.last_name, c.role, c.company_id,
                      co.name AS company_name, co.location AS company_location
               FROM drafts d
               JOIN contacts c ON c.id = d.contact_id
               LEFT JOIN companies co ON co.id = c.company_id
               WHERE d.status = 'queued' AND c.status = ?
                 AND COALESCE(c.lane, '') = ''
                 AND COALESCE(d.gmail_message_id, '') = ''
               ORDER BY """ + NAMED_FIRST + """,
                        COALESCE(co.priority, 0) DESC, c.confidence DESC, d.id
               LIMIT ?""",
            (QUEUED, limit),
        ).fetchall()

    def stale_mirrors(self) -> list[sqlite3.Row]:
        """Copies in Gmail whose draft is no longer waiting to be sent.

        Either this bot has sent it, or it was skipped or failed. In every
        case the copy in Drafts is now something that must not be sent by
        hand, so it has to go.
        """
        return self.conn.execute(
            """SELECT d.id, d.gmail_message_id, c.email
               FROM drafts d JOIN contacts c ON c.id = d.contact_id
               WHERE COALESCE(d.gmail_message_id, '') != '' AND d.status != 'queued'"""
        ).fetchall()

    def set_draft_mirror(self, draft_id: int, message_id: str) -> None:
        self.conn.execute(
            "UPDATE drafts SET gmail_message_id = ? WHERE id = ?",
            (message_id, draft_id),
        )
        self.conn.commit()

    def clear_draft_mirror(self, draft_id: int) -> None:
        self.conn.execute(
            "UPDATE drafts SET gmail_message_id = '' WHERE id = ?", (draft_id,)
        )
        self.conn.commit()

    # ---------- follow-ups ----------

    def due_for_followup(self, after_days: int, max_total_sends: int,
                         limit: int | None = 50) -> list[sqlite3.Row]:
        """Contacts who were written to, never answered, and are due a nudge.

        Anyone who replied, bounced or opted out is excluded by the status and
        suppression checks, so a follow-up can never chase someone who already
        said no.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=after_days)).isoformat()
        return self.conn.execute(
            """SELECT c.id, c.email, c.first_name, c.last_name, c.role,
                      c.verify_status, c.provenance, c.lane AS contact_lane,
                      co.name AS company_name, co.location AS company_location,
                      co.one_liner, co.description,
                      COUNT(s.id) AS send_count,
                      MAX(s.sent_at) AS last_sent_at,
                      (SELECT message_id FROM sends s2
                        WHERE s2.contact_id = c.id AND s2.status = 'sent'
                        ORDER BY s2.id LIMIT 1) AS first_message_id,
                      (SELECT subject FROM sends s3
                        WHERE s3.contact_id = c.id AND s3.status = 'sent'
                        ORDER BY s3.id LIMIT 1) AS first_subject
               FROM contacts c
               -- A "sending" row is an attempt whose outcome was never
               -- recorded; it counts, so the bump is never repeated.
               JOIN sends s ON s.contact_id = c.id AND s.status IN ('sent', 'sending')
               LEFT JOIN companies co ON co.id = c.company_id
               WHERE c.status = ?
                 AND c.email NOT IN (SELECT email FROM suppression)
                 -- A colleague answered or opted out, so the company has
                 -- spoken and a bump to this person would talk over them.
                 AND NOT EXISTS (
                     SELECT 1 FROM contacts o
                     LEFT JOIN suppression os ON os.email = o.email
                     WHERE o.company_id = c.company_id AND o.id <> c.id
                       AND (o.status = ? OR os.reason = 'opt-out reply'))
                 -- Bumped by the account that wrote first, so it threads.
                 AND COALESCE((SELECT s5.lane FROM sends s5
                                WHERE s5.contact_id = c.id
                                  AND s5.status IN ('sent', 'sending')
                                ORDER BY s5.id LIMIT 1), ?) = ?
               GROUP BY c.id
               HAVING COUNT(s.id) < ? AND MAX(s.sent_at) <= ?
               ORDER BY MAX(s.sent_at) LIMIT ?""",
            (SENT, REPLIED, MAIN_LANE, self.lane, max_total_sends, cutoff,
             -1 if limit is None else limit),
        ).fetchall()

    # ---------- sends ----------

    def record_send(self, contact_id: int, email: str, subject: str, message_id: str,
                    backend: str, local_date: str, status: str = "sent",
                    error: str = "") -> None:
        self.conn.execute(
            """INSERT INTO sends
               (contact_id, email, subject, message_id, backend, status, error,
                sent_at, local_date, lane)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (contact_id, email.lower(), subject, message_id, backend, status,
             error, _now(), local_date, self.lane),
        )
        if status == "sent":
            self.conn.execute(
                "UPDATE contacts SET status = ? WHERE id = ?", (SENT, contact_id)
            )
        self.conn.commit()

    def begin_send(self, contact_id: int, email: str, subject: str,
                   local_date: str) -> int:
        """Record an attempt, committed, before the message leaves.

        If the write after SMTP accepts the message fails (a full disk, a
        lock held past the timeout), this row is what stops the next tick
        mailing the same person again: "sending" counts as mailed wherever
        a prior send is checked. Unknown outcome means never resend.
        """
        cur = self.conn.execute(
            """INSERT INTO sends
               (contact_id, email, subject, message_id, backend, status, error,
                sent_at, local_date, lane)
               VALUES (?,?,?,'','','sending','',?,?,?)""",
            (contact_id, email.lower(), subject, _now(), local_date, self.lane),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_send(self, send_id: int, contact_id: int, message_id: str,
                    backend: str, ok: bool, error: str = "") -> None:
        self.conn.execute(
            "UPDATE sends SET message_id = ?, backend = ?, status = ?, error = ?, "
            "sent_at = ? WHERE id = ?",
            (message_id, backend, "sent" if ok else "failed", error, _now(), send_id),
        )
        if ok:
            self.conn.execute(
                "UPDATE contacts SET status = ? WHERE id = ?", (SENT, contact_id))
        self.conn.commit()

    def sent_count_on(self, local_date: str) -> int:
        """Messages this lane has put on the wire on a day, attempts included."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM sends WHERE local_date = ? "
            "AND status IN ('sent', 'sending') AND lane = ?",
            (local_date, self.lane),
        ).fetchone()
        return int(row["n"])

    def record_cap(self, local_date: str, cap: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO daily_caps (lane, local_date, cap) VALUES (?,?,?)",
            (self.lane, local_date, int(cap)))
        self.conn.commit()

    def lane_at_cap(self, lane: str, local_date: str) -> tuple[bool, str]:
        """Whether another lane has used its whole cap today, and its tally.

        A lane that has not run today has no cap on record and counts as
        not full: the open queue is its to send first.
        """
        row = self.conn.execute(
            "SELECT cap FROM daily_caps WHERE lane = ? AND local_date = ?",
            (lane, local_date)).fetchone()
        if row is None:
            return False, f"{lane} has not run today"
        sent = int(self.conn.execute(
            "SELECT COUNT(*) FROM sends WHERE lane = ? AND local_date = ? "
            "AND status IN ('sent', 'sending')", (lane, local_date)).fetchone()[0])
        return sent >= int(row["cap"]), f"{lane} at {sent}/{row['cap']}"

    def followup_count_on(self, local_date: str) -> int:
        """Of those, how many were bumps: a send to someone already written to."""
        row = self.conn.execute(
            """SELECT COUNT(*) AS n FROM sends s
               WHERE s.local_date = ? AND s.status IN ('sent', 'sending')
                 AND s.lane = ?
                 AND EXISTS (SELECT 1 FROM sends p
                             WHERE p.contact_id = s.contact_id AND p.id < s.id
                               AND p.status IN ('sent', 'sending'))""",
            (local_date, self.lane),
        ).fetchone()
        return int(row["n"])

    def active_send_days(self, before_local_date: str) -> int:
        """How many distinct days the bot sent on, before the given date.

        Days sent on, not calendar days elapsed: a weekend, an outage or a
        week with an empty queue must not hand the ramp credit it has not
        earned. Excluding today keeps today's cap fixed for the whole day.
        """
        row = self.conn.execute(
            """SELECT COUNT(DISTINCT local_date) AS n FROM sends
               WHERE status = 'sent' AND local_date < ? AND lane = ?""",
            (before_local_date, self.lane),
        ).fetchone()
        return int(row["n"])

    def recent_bounce_pct(self, window: int = 100,
                          before_local_date: str = "") -> float:
        """Recurrable bounce rate over the most recent `window` sends, as a
        percentage.

        The inbox scan records a bounce on the contact rather than on the
        send row, so this joins back to contacts to count them.

        Only bounces whose class of address the gates would still mail today
        are counted: a personal mailbox that is verified, or catch-all with
        trusted provenance. A bounce from a class the gates now refuse cannot
        happen again, so holding the cap for it protects nothing; in October
        that kept the ramp at the floor for weeks after the cause was fixed.
        A bounce from a class still being sent to predicts more like it and
        keeps holding. The check uses the same functions the send path does,
        so loosening a gate automatically brings its bounces back into the
        count. Bounced contacts are excluded from reprobing, which means
        their verify_status still says what it said when the send was allowed.

        With `before_local_date`, only sends from earlier days count, so the
        figure holds still for the whole of that day. Counting today's sends
        let each clean delivery dilute the rate under the limit and lift the
        hold mid-morning: one day went 18 out against a cap of 8.

        Per lane: each account's reputation is its own, so each brake reads
        only the sends that account made, judged by what that lane may send.
        """
        rows = self.conn.execute(
            """SELECT c.email, c.status, c.verify_status, c.provenance, c.lane
               FROM (SELECT contact_id FROM sends
                     WHERE status = 'sent' AND lane = ?
                       AND (? = '' OR local_date < ?)
                     ORDER BY id DESC LIMIT ?) s
               JOIN contacts c ON c.id = s.contact_id""",
            (self.lane, before_local_date, before_local_date, max(1, window)),
        ).fetchall()
        if not rows:
            return 0.0
        recurrable = sum(
            1 for r in rows
            if r["status"] == BOUNCED
            and sendable_address_class(
                r["email"], r["verify_status"], r["provenance"],
                r["lane"] or "", self.lane)
        )
        return 100.0 * recurrable / len(rows)

    def max_daily_sends(self, days: int = 3, before_local_date: str = "") -> int:
        """The busiest of the last `days` days actually sent on.

        Feeds the ramp's growth limiter: how much volume this address has
        recently proven it can carry. Counted over send days rather than
        calendar days so a weekend or an outage does not shrink it.
        """
        row = self.conn.execute(
            """SELECT MAX(n) FROM (
                 SELECT COUNT(*) AS n FROM sends
                 WHERE status = 'sent' AND lane = ? AND (? = '' OR local_date < ?)
                 GROUP BY local_date
                 ORDER BY local_date DESC LIMIT ?)""",
            (self.lane, before_local_date, before_local_date, max(1, days)),
        ).fetchone()
        return int(row[0] or 0)

    def bounces_on(self, local_date: str) -> tuple[int, list[sqlite3.Row]]:
        """How many went out on a day, and which of them have since bounced.

        Attributed to the day the message was sent rather than the day the
        failure notice arrived, because the question being asked is "was
        anything wrong with what we sent that day". Notices normally land
        within seconds, so by the time the window closes the picture is
        complete; one that arrives later simply lands in the next day's count.
        """
        total = int(self.conn.execute(
            "SELECT COUNT(*) FROM sends WHERE local_date = ? AND status = 'sent' "
            "AND lane = ?",
            (local_date, self.lane),
        ).fetchone()[0])
        rows = self.conn.execute(
            """SELECT DISTINCT s.email, c.confidence, c.verify_status,
                      c.verify_detail, c.provenance, co.name AS company_name
               FROM sends s
               JOIN contacts c ON c.id = s.contact_id
               LEFT JOIN companies co ON co.id = c.company_id
               WHERE s.local_date = ? AND s.status = 'sent' AND c.status = ?
                 AND s.lane = ?
               ORDER BY s.id""",
            (local_date, BOUNCED, self.lane),
        ).fetchall()
        return total, rows

    def sends_on(self, local_date: str) -> list[sqlite3.Row]:
        """This lane's sends on a day."""
        return self.conn.execute(
            """SELECT s.*, co.name AS company_name, c.first_name, c.role
               FROM sends s
               JOIN contacts c ON c.id = s.contact_id
               LEFT JOIN companies co ON co.id = c.company_id
               WHERE s.local_date = ? AND s.lane = ? ORDER BY s.id""",
            (local_date, self.lane),
        ).fetchall()

    def find_contact_by_email(self, email: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM contacts WHERE email = ?", (email.lower(),)
        ).fetchone()

    def contact_for_thread(self, message_ids: list[str]) -> Optional[sqlite3.Row]:
        """The contact whose sent message one of these Message-IDs names.

        Lets a reply be recognised when it comes from a different address
        than the one written to, as when gc@ answers the note sent to git@.
        """
        ids = [m.strip() for m in message_ids if m.strip()]
        if not ids:
            return None
        marks = ",".join("?" * len(ids))
        return self.conn.execute(
            f"""SELECT c.* FROM sends s JOIN contacts c ON c.id = s.contact_id
                WHERE s.message_id IN ({marks}) LIMIT 1""",
            ids,
        ).fetchone()

    def last_contacted_at_domain(self, domain: str) -> Optional[sqlite3.Row]:
        """The person most recently written to at this mail domain, if any."""
        return self.conn.execute(
            """SELECT c.* FROM sends s JOIN contacts c ON c.id = s.contact_id
               WHERE s.status = 'sent'
                 AND substr(c.email, instr(c.email, '@') + 1) = ?
               ORDER BY s.id DESC LIMIT 1""",
            (domain.lower(),),
        ).fetchone()

    # ---------- domain cache ----------

    def cache_domain(self, domain: str, mx_host: str, catchall: bool) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO domain_cache (domain, mx_host, catchall, checked_at)
               VALUES (?,?,?,?)""",
            (domain.lower(), mx_host, int(catchall), _now()),
        )
        self.conn.commit()

    def cached_domain(self, domain: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM domain_cache WHERE domain = ?", (domain.lower(),)
        ).fetchone()

    def clear_unproven_catchall(self) -> int:
        """Drop cached "not a catch-all" verdicts so they get proved again.

        A catch-all verdict needed a real 250 to be recorded, so those rows are
        evidence. The opposite verdict used to be written even when the probe
        got no answer at all, which is a guess, and mx_host reloads it forever.
        There is no way to tell the sound rows from the poisoned ones after the
        fact, so every negative is dropped and re-probed on demand.
        """
        cur = self.conn.execute("DELETE FROM domain_cache WHERE catchall = 0")
        self.conn.commit()
        return cur.rowcount

    # ---------- recovery ----------

    # Verdicts that mean "the mailbox server never actually answered". They are
    # not findings about the address, only a record of a probe that failed, so
    # they are worth retrying once probing works again.
    #
    # Unanchored on purpose. The HN importer prefixes its own provenance, so
    # the verdict reads "published on HN: no SMTP answer (0); ...", and a
    # pattern anchored at the start of the string skipped every one of them.
    UNPROVEN_DETAILS = ("%no SMTP answer%", "%inconclusive%", "%probing paused%")

    # Both states carry outage-era verdicts. A queued contact already has a
    # rendered draft waiting to be sent, which makes it the more urgent of the
    # two: leaving those unchecked is what put two dead addresses on the wire.
    REVERIFY_STATES = (PENDING, QUEUED)

    def contacts_needing_reverify(self, limit: int = 0) -> list[sqlite3.Row]:
        """Contacts whose verdict records a failed probe rather than a mailbox.

        Queued first, because a draft already exists for those and the next
        send tick will put it on the wire.
        """
        clauses = " OR ".join("c.verify_detail LIKE ?" for _ in self.UNPROVEN_DETAILS)
        states = ",".join("?" for _ in self.REVERIFY_STATES)
        sql = (
            "SELECT c.*, co.name AS company_name FROM contacts c "
            "LEFT JOIN companies co ON co.id = c.company_id "
            f"WHERE c.status IN ({states}) AND ({clauses}) "
            "ORDER BY (c.status = ?) DESC, COALESCE(co.priority, 0) DESC, "
            "c.confidence DESC, c.id"
        )
        args: list = [*self.REVERIFY_STATES, *self.UNPROVEN_DETAILS, QUEUED]
        if limit:
            sql += " LIMIT ?"
            args.append(limit)
        return self.conn.execute(sql, tuple(args)).fetchall()

    # Failures that were really the prober giving up rather than the company
    # being unreachable. A company parked for one of these deserves another
    # pass now that probing works.
    #
    # SKIP_NO_MX stays out: a domain that answers for itself and names no mail
    # host is a finding, and reopening those every run would spend the daily
    # fetch budget re-proving it. SKIP_MX_UNRESOLVED is the opposite case, a
    # lookup that never came back, so it belongs here.
    RETRYABLE_SKIPS = (
        "no candidate address accepted",
        SKIP_CATCHALL_UNPROVABLE,
        SKIP_MX_UNRESOLVED,
        SKIP_PROBE_UNAVAILABLE,
    )

    # The subset that says nothing at all about the company, only that a
    # lookup or probe went unanswered. Cheap to reopen on every relay run,
    # unlike the two findings above, which would churn the fetch budget.
    UNANSWERED_SKIPS = (SKIP_MX_UNRESOLVED, SKIP_PROBE_UNAVAILABLE)

    def drop_draft_for_contact(self, contact_id: int) -> int:
        """Take a rendered draft out of play after its address proved dead.

        Suppression alone already stops the scheduled send, because
        queued_drafts joins on the contact still being queued. This also marks
        the draft itself, which is what makes stale_mirrors pick it up so the
        copy sitting in Gmail Drafts is deleted too. Without that the copy
        stays on the phone, one tap from being sent by hand.
        """
        cur = self.conn.execute(
            "UPDATE drafts SET status = 'skipped' WHERE contact_id = ? "
            "AND status = 'queued'",
            (contact_id,),
        )
        self.conn.commit()
        return cur.rowcount

    def reopen_for_enrich(self, reasons: Iterable[str] = ()) -> int:
        """Put companies parked on a retryable reason back into the enrich queue."""
        reasons = tuple(reasons) or self.RETRYABLE_SKIPS
        marks = ",".join("?" for _ in reasons)
        cur = self.conn.execute(
            f"""UPDATE companies SET status = 'new', skip_reason = ''
                WHERE status = ? AND skip_reason IN ({marks})""",
            (NO_CONTACTS, *reasons),
        )
        self.conn.commit()
        return cur.rowcount

    def companies_parked_on(self, reason: str) -> list[sqlite3.Row]:
        """Companies retired for one exact reason, most promising first."""
        return self.conn.execute(
            """SELECT * FROM companies WHERE status = ? AND skip_reason = ?
               ORDER BY priority DESC, is_hiring DESC, id""",
            (NO_CONTACTS, reason),
        ).fetchall()

    def reopen_company(self, company_id: int) -> None:
        """Put one company back in the enrich queue, its reason cleared."""
        self.conn.execute(
            "UPDATE companies SET status = 'new', skip_reason = '' WHERE id = ?",
            (company_id,),
        )
        self.conn.commit()

    # ---------- misc ----------

    def log(self, kind: str, ref: str = "", detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO events (kind, ref, detail, created_at) VALUES (?,?,?,?)",
            (kind, ref, detail, _now()),
        )
        self.conn.commit()

    def stats(self) -> dict[str, int]:
        def one(sql: str, args: tuple = ()) -> int:
            return int(self.conn.execute(sql, args).fetchone()[0])

        return {
            "companies": one("SELECT COUNT(*) FROM companies"),
            "companies_enriched": one(
                "SELECT COUNT(*) FROM companies WHERE status = ?", (ENRICHED,)
            ),
            "companies_no_contacts": one(
                "SELECT COUNT(*) FROM companies WHERE status = ?", (NO_CONTACTS,)
            ),
            "contacts": one("SELECT COUNT(*) FROM contacts"),
            "contacts_verified": one(
                "SELECT COUNT(*) FROM contacts WHERE verify_status = ?", (VERIFIED,)
            ),
            "contacts_catchall": one(
                "SELECT COUNT(*) FROM contacts WHERE verify_status = ?", (CATCHALL,)
            ),
            "queued": one("SELECT COUNT(*) FROM contacts WHERE status = ?", (QUEUED,)),
            "sent": one("SELECT COUNT(*) FROM sends WHERE status = 'sent'"),
            "bounced": one("SELECT COUNT(*) FROM contacts WHERE status = ?", (BOUNCED,)),
            "suppressed": one("SELECT COUNT(*) FROM suppression"),
        }
