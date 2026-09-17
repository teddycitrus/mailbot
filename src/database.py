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
    BOUNCED, CATCHALL, Company, Contact, ENRICHED, NO_CONTACTS,
    QUEUED, REPLIED, SENT, SUPPRESSED, VERIFIED,
)

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
    created_at    TEXT NOT NULL
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
    local_date  TEXT NOT NULL
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


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.conn = sqlite3.connect(self.path)
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
        ):
            have = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"
                )
        self.conn.commit()

    def upsert_company(self, c: Company) -> int:
        """Insert, or return the existing id. Domain is the identity key."""
        row = self.conn.execute(
            "SELECT id FROM companies WHERE domain = ?", (c.domain,)
        ).fetchone()
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
                confidence, source, verify_status, verify_detail, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (c.company_id, email, c.first_name, c.last_name, c.role, c.bio,
             c.linkedin, c.confidence, c.source, c.verify_status, c.verify_detail,
             c.status, _now()),
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
               ORDER BY COALESCE(co.priority, 0) DESC, c.confidence DESC, c.id
               LIMIT ?""",
            (status, limit),
        ).fetchall()

    def company_has_contacted(self, company_id: int) -> bool:
        """One person per company keeps the outreach from looking like a blast."""
        return self.conn.execute(
            """SELECT 1 FROM contacts
               WHERE company_id = ? AND status IN (?,?,?,?)""",
            (company_id, SENT, REPLIED, BOUNCED, QUEUED),
        ).fetchone() is not None

    # ---------- the duplicate-send gate ----------

    def can_send_to(self, email: str) -> tuple[bool, str]:
        """Single source of truth for whether an address may receive mail."""
        email = email.lower().strip()
        if self.is_suppressed(email):
            return False, "suppressed"
        row = self.conn.execute(
            "SELECT status FROM contacts WHERE email = ?", (email,)
        ).fetchone()
        if row and row["status"] in {SENT, REPLIED, BOUNCED, SUPPRESSED}:
            return False, f"already {row['status']}"
        prior = self.conn.execute(
            "SELECT 1 FROM sends WHERE email = ? AND status = 'sent'", (email,)
        ).fetchone()
        if prior:
            return False, "already in send log"
        return True, ""

    def can_send_to_again(self, email: str) -> tuple[bool, str]:
        """The follow-up gate: like can_send_to, but a prior send is expected.

        Everything that means "leave this person alone" still blocks. Only the
        "already contacted once" rule is relaxed, and only for someone whose
        status is still exactly SENT.
        """
        email = email.lower().strip()
        if self.is_suppressed(email):
            return False, "suppressed"
        row = self.conn.execute(
            "SELECT status FROM contacts WHERE email = ?", (email,)
        ).fetchone()
        if row is None:
            return False, "unknown contact"
        if row["status"] in {REPLIED, BOUNCED, SUPPRESSED}:
            return False, f"already {row['status']}"
        if row["status"] != SENT:
            return False, f"status is {row['status']}, not sent"
        return True, ""

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

    def queued_drafts(self, limit: int = 100) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT d.*, c.email, c.first_name, c.last_name, c.role,
                      co.name AS company_name, co.location AS company_location
               FROM drafts d
               JOIN contacts c ON c.id = d.contact_id
               LEFT JOIN companies co ON co.id = c.company_id
               WHERE d.status = 'queued' AND c.status = ?
               ORDER BY COALESCE(co.priority, 0) DESC, c.confidence DESC, d.id
               LIMIT ?""",
            (QUEUED, limit),
        ).fetchall()

    def mark_draft(self, draft_id: int, status: str) -> None:
        self.conn.execute("UPDATE drafts SET status = ? WHERE id = ?", (status, draft_id))
        self.conn.commit()

    def queued_draft_count(self) -> int:
        """How deep the queue is, for the top-up target in queue()."""
        return int(self.conn.execute(
            """SELECT COUNT(*) FROM drafts d JOIN contacts c ON c.id = d.contact_id
               WHERE d.status = 'queued' AND c.status = ?""",
            (QUEUED,),
        ).fetchone()[0])

    # ---------- Gmail Drafts mirror ----------

    def drafts_to_mirror(self, limit: int = 50) -> list[sqlite3.Row]:
        """Queued drafts that have no copy in the Gmail Drafts folder yet."""
        return self.conn.execute(
            """SELECT d.*, c.email, c.first_name, c.last_name, c.role,
                      co.name AS company_name, co.location AS company_location
               FROM drafts d
               JOIN contacts c ON c.id = d.contact_id
               LEFT JOIN companies co ON co.id = c.company_id
               WHERE d.status = 'queued' AND c.status = ?
                 AND COALESCE(d.gmail_message_id, '') = ''
               ORDER BY COALESCE(co.priority, 0) DESC, c.confidence DESC, d.id
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
                         limit: int = 50) -> list[sqlite3.Row]:
        """Contacts who were written to, never answered, and are due a nudge.

        Anyone who replied, bounced or opted out is excluded by the status and
        suppression checks, so a follow-up can never chase someone who already
        said no.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=after_days)).isoformat()
        return self.conn.execute(
            """SELECT c.id, c.email, c.first_name, c.last_name, c.role,
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
               JOIN sends s ON s.contact_id = c.id AND s.status = 'sent'
               LEFT JOIN companies co ON co.id = c.company_id
               WHERE c.status = ?
                 AND c.email NOT IN (SELECT email FROM suppression)
               GROUP BY c.id
               HAVING COUNT(s.id) < ? AND MAX(s.sent_at) <= ?
               ORDER BY MAX(s.sent_at) LIMIT ?""",
            (SENT, max_total_sends, cutoff, limit),
        ).fetchall()

    # ---------- sends ----------

    def record_send(self, contact_id: int, email: str, subject: str, message_id: str,
                    backend: str, local_date: str, status: str = "sent",
                    error: str = "") -> None:
        self.conn.execute(
            """INSERT INTO sends
               (contact_id, email, subject, message_id, backend, status, error,
                sent_at, local_date)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (contact_id, email.lower(), subject, message_id, backend, status,
             error, _now(), local_date),
        )
        if status == "sent":
            self.conn.execute(
                "UPDATE contacts SET status = ? WHERE id = ?", (SENT, contact_id)
            )
        self.conn.commit()

    def sent_count_on(self, local_date: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM sends WHERE local_date = ? AND status = 'sent'",
            (local_date,),
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
               WHERE status = 'sent' AND local_date < ?""",
            (before_local_date,),
        ).fetchone()
        return int(row["n"])

    def recent_bounce_pct(self, window: int = 100) -> float:
        """Bounce rate over the most recent `window` sends, as a percentage.

        The inbox scan records a bounce on the contact rather than on the
        send row, so this joins back to contacts to count them.
        """
        row = self.conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN c.status = ? THEN 1 ELSE 0 END) AS bounced
               FROM (SELECT contact_id FROM sends
                     WHERE status = 'sent' ORDER BY id DESC LIMIT ?) s
               JOIN contacts c ON c.id = s.contact_id""",
            (BOUNCED, max(1, window)),
        ).fetchone()
        total = int(row["total"] or 0)
        if not total:
            return 0.0
        return 100.0 * int(row["bounced"] or 0) / total

    def sends_on(self, local_date: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT s.*, co.name AS company_name, c.first_name, c.role
               FROM sends s
               JOIN contacts c ON c.id = s.contact_id
               LEFT JOIN companies co ON co.id = c.company_id
               WHERE s.local_date = ? ORDER BY s.id""",
            (local_date,),
        ).fetchall()

    def find_contact_by_email(self, email: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM contacts WHERE email = ?", (email.lower(),)
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
