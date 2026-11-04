"""Offline scorecard computed from a read-only DB snapshot.

Measures what the live system has actually done: label quality, bounce and
reply rates by segment with confidence intervals, what the current gates
would have refused among past sends, how the send ramp behaved day by day,
and how deep the remaining queue is. Nothing here touches the network or
writes to the database; the connection is opened read-only and the output
goes to files in a directory outside the repo.

Usage:
    python -m src.scorecard --db PATH --out DIR [--split DATE] [--holdout]

Caveat printed into every report: contacts.verify_status is rewritten by
reprobing and contacts.provenance was backfilled on 2026-11-01, so segment
tables reflect today's classification of each address, not necessarily what
was known at send time. Sends, events and suppression rows are append-only
and are trustworthy history.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

from .inbox import OPT_OUT
from .models import ROLE_LOCALS
from .verifier import (
    domain_of, is_personal_mailbox, local_part, sendable_address_class,
)

# Sends strictly before this date are the training period; the rest is the
# holdout. Segment tables are computed on the training period unless
# --holdout is passed, and every holdout read is logged, because a holdout
# that gets consulted on every decision stops being one.
DEFAULT_SPLIT = "2026-10-20"

MX_PROVIDERS = (
    ("google", ("google", "aspmx")),
    ("microsoft", ("outlook", "mail.protection")),
    ("proofpoint", ("pphosted", "ppe-hosted")),
    ("mimecast", ("mimecast",)),
    ("zoho", ("zoho",)),
    ("improvmx", ("improvmx",)),
    ("cloudflare", ("cloudflare", "mxrecord.io")),
    ("fastmail", ("messagingengine", "fastmail")),
    ("icloud", ("icloud",)),
    ("mailgun", ("mailgun",)),
    ("amazon-ses", ("amazonaws",)),
)


def wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a proportion, as percentages."""
    if total <= 0:
        return 0.0, 0.0
    p = successes / total
    denom = 1 + z * z / total
    centre = p + z * z / (2 * total)
    spread = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    return (100 * (centre - spread) / denom, 100 * (centre + spread) / denom)


def open_ro(db_path: str | Path) -> sqlite3.Connection:
    uri = f"file:{Path(db_path).resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def mx_provider(mx_host: str) -> str:
    host = (mx_host or "").lower()
    if not host:
        return "unknown"
    for name, needles in MX_PROVIDERS:
        if any(n in host for n in needles):
            return name
    return "self-hosted/other"


def gate_would_refuse(email: str, verify_status: str, provenance: str) -> str:
    """The address-quality half of Database.can_send_to, for replay.

    The DB-state half (suppression, already sent) always fires on a past
    send, so replaying it would say nothing. This half is what decides
    whether a new address like this one would be mailed today. The verdict
    comes from the same function the send path and the ramp use; only the
    reason wording is local.
    """
    if sendable_address_class(email, verify_status, provenance):
        return ""
    if not is_personal_mailbox(email):
        return "shared or blocked inbox"
    return f"not deliverable ({verify_status}/{provenance or 'no provenance'})"


# --------------------------------------------------------------- send table

def send_table(conn: sqlite3.Connection) -> list[dict]:
    """One row per delivered send, joined to today's view of the contact.

    Bounce and reply are attributed to a specific send: the latest send to
    that contact at or before the first matching event. A label with no
    event behind it is attributed to the contact's last send and flagged
    estimated, so label error stays visible.
    """
    sends = [dict(r) for r in conn.execute(
        """SELECT s.id, s.contact_id, s.email, s.sent_at, s.local_date,
                  c.first_name, c.role, c.confidence, c.source AS contact_source,
                  c.verify_status, c.provenance, c.status AS contact_status,
                  c.company_id,
                  co.source AS company_source, co.batch, co.employees,
                  co.location, co.domain AS company_domain
           FROM sends s
           JOIN contacts c ON c.id = s.contact_id
           LEFT JOIN companies co ON co.id = c.company_id
           WHERE s.status = 'sent'
           ORDER BY s.id"""
    )]

    first_event: dict[str, dict[str, str]] = {"bounce": {}, "reply": {}}
    for kind in ("bounce", "reply"):
        for row in conn.execute(
            "SELECT ref, MIN(created_at) AS t FROM events WHERE kind = ? GROUP BY ref",
            (kind,),
        ):
            first_event[kind][row["ref"].lower()] = row["t"]
    refused = refusals_logged_as_replies(conn)

    mx_by_domain = {
        row["domain"]: mx_provider(row["mx_host"])
        for row in conn.execute("SELECT domain, mx_host FROM domain_cache")
    }

    by_contact: dict[int, list[dict]] = {}
    for send in sends:
        by_contact.setdefault(send["contact_id"], []).append(send)

    for send in sends:
        touches = by_contact[send["contact_id"]]
        send["touch_no"] = touches.index(send) + 1
        send["bounced"] = False
        send["replied"] = False
        send["label_estimated"] = False
        send["mx"] = mx_by_domain.get(domain_of(send["email"]), "unknown")
        send["gate_refusal"] = gate_would_refuse(
            send["email"], send["verify_status"], send["provenance"])

    def attribute(kind: str, status_value: str, flag: str) -> None:
        for contact_id, touches in by_contact.items():
            email = touches[0]["email"].lower()
            if kind == "reply" and email in refused:
                continue
            event_at = first_event[kind].get(email)
            if event_at:
                target = None
                for send in touches:
                    if send["sent_at"] <= event_at:
                        target = send
                target = target or touches[0]
                target[flag] = True
            elif touches[0]["contact_status"] == status_value:
                touches[-1][flag] = True
                touches[-1]["label_estimated"] = True

    attribute("bounce", "bounced", "bounced")
    attribute("reply", "replied", "replied")
    return sends


def refusals_logged_as_replies(conn: sqlite3.Connection) -> set[str]:
    """Addresses whose "reply" was a refusal, as its logged subject shows.

    Until the inbox read subjects, the mail client's List-Unsubscribe message
    (subject "unsubscribe") was recorded as a reply. Those are opt-outs.
    """
    return {row["ref"].lower() for row in conn.execute(
        "SELECT ref, detail FROM events WHERE kind = 'reply'")
        if OPT_OUT.search(row["detail"] or "")}


# -------------------------------------------------------------- label audit

def label_audit(conn: sqlite3.Connection) -> dict:
    out: dict = {}
    one = lambda sql, p=(): conn.execute(sql, p).fetchone()[0]  # noqa: E731

    out["bounced_contacts"] = one("SELECT COUNT(*) FROM contacts WHERE status='bounced'")
    out["bounce_event_refs"] = one("SELECT COUNT(DISTINCT ref) FROM events WHERE kind='bounce'")
    out["bounce_events_total"] = one("SELECT COUNT(*) FROM events WHERE kind='bounce'")
    out["bounce_refs_with_repeats"] = one(
        """SELECT COUNT(*) FROM (SELECT ref FROM events WHERE kind='bounce'
           GROUP BY ref HAVING COUNT(*) > 1)""")
    out["last_repeat_bounce_event"] = conn.execute(
        """SELECT MAX(created_at) FROM events WHERE kind='bounce' AND ref IN
           (SELECT ref FROM events WHERE kind='bounce' GROUP BY ref HAVING COUNT(*) > 1)"""
    ).fetchone()[0]
    out["bounced_without_event"] = one(
        """SELECT COUNT(*) FROM contacts c WHERE c.status='bounced' AND NOT EXISTS
           (SELECT 1 FROM events e WHERE e.kind='bounce' AND lower(e.ref)=lower(c.email))""")
    out["bounce_event_but_status_overwritten"] = one(
        """SELECT COUNT(*) FROM (SELECT DISTINCT e.ref FROM events e
           JOIN contacts c ON lower(c.email)=lower(e.ref)
           WHERE e.kind='bounce' AND c.status != 'bounced')""")

    out["replied_contacts"] = one("SELECT COUNT(*) FROM contacts WHERE status='replied'")
    out["reply_event_refs"] = one("SELECT COUNT(DISTINCT ref) FROM events WHERE kind='reply'")
    out["replied_without_event"] = one(
        """SELECT COUNT(*) FROM contacts c WHERE c.status='replied' AND NOT EXISTS
           (SELECT 1 FROM events e WHERE e.kind='reply' AND lower(e.ref)=lower(c.email))""")

    out["optout_suppressions"] = one(
        "SELECT COUNT(*) FROM suppression WHERE reason='opt-out reply'")
    out["optout_events"] = one("SELECT COUNT(*) FROM events WHERE kind='opt_out'")
    out["refusals_logged_as_replies"] = len(refusals_logged_as_replies(conn))
    out["suppression_by_reason"] = {
        row["reason"]: row["n"] for row in conn.execute(
            "SELECT reason, COUNT(*) AS n FROM suppression GROUP BY reason")}
    out["failed_sends"] = one("SELECT COUNT(*) FROM sends WHERE status != 'sent'")

    mailed_suppressed = one(
        """SELECT COUNT(DISTINCT s.contact_id) FROM sends s
           JOIN contacts c ON c.id = s.contact_id
           JOIN suppression su ON su.email = lower(c.email)
           WHERE c.status NOT IN ('bounced', 'replied')""")
    out["mailed_contacts_now_suppressed"] = mailed_suppressed
    return out


# ------------------------------------------------------------ segment rates

def _bucket_employees(n) -> str:
    if n is None:
        return "unknown"
    if n <= 10:
        return "1-10"
    if n <= 50:
        return "11-50"
    if n <= 200:
        return "51-200"
    return "200+"


def _bucket_location(loc: str) -> str:
    low = (loc or "").lower()
    if not low:
        return "unknown"
    if "toronto" in low or "canada" in low:
        return "canada"
    if "san francisco" in low or "bay area" in low or ", ca" in low:
        return "sf-bay"
    if "new york" in low or ", ny" in low:
        return "new-york"
    return "other"


SEGMENTS = {
    "provenance_verify": lambda s: f"{s['provenance'] or 'none'}/{s['verify_status']}",
    "company_source": lambda s: s["company_source"] or "unknown",
    "verify_status": lambda s: s["verify_status"],
    "mx_provider": lambda s: s["mx"],
    "touch": lambda s: "first" if s["touch_no"] == 1 else "follow-up",
    "role": lambda s: (s["role"] or "unknown").lower()[:24],
    "company_size": lambda s: _bucket_employees(s["employees"]),
    "location": lambda s: _bucket_location(s["location"]),
    "batch": lambda s: s["batch"] or "none",
}


def segment_rates(sends: list[dict]) -> dict:
    tables: dict = {}
    for name, keyfn in SEGMENTS.items():
        groups: dict[str, list[dict]] = {}
        for send in sends:
            groups.setdefault(keyfn(send), []).append(send)
        rows = []
        for key, members in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            n = len(members)
            b = sum(1 for m in members if m["bounced"])
            r = sum(1 for m in members if m["replied"])
            rows.append({
                "segment": key, "sends": n,
                "bounced": b, "bounce_pct": round(100 * b / n, 1),
                "bounce_ci": [round(x, 1) for x in wilson(b, n)],
                "replied": r, "reply_pct": round(100 * r / n, 1),
                "reply_ci": [round(x, 1) for x in wilson(r, n)],
            })
        tables[name] = rows
    return tables


def company_rates(sends: list[dict]) -> dict:
    companies: dict = {}
    for send in sends:
        key = send["company_domain"] or send["email"].split("@")[1]
        c = companies.setdefault(key, {"bounced": False, "replied": False})
        c["bounced"] = c["bounced"] or send["bounced"]
        c["replied"] = c["replied"] or send["replied"]
    n = len(companies)
    b = sum(1 for c in companies.values() if c["bounced"])
    r = sum(1 for c in companies.values() if c["replied"])
    return {
        "companies_mailed": n,
        "bounced": b, "bounce_pct": round(100 * b / n, 1) if n else 0,
        "bounce_ci": [round(x, 1) for x in wilson(b, n)],
        "replied": r, "reply_pct": round(100 * r / n, 1) if n else 0,
        "reply_ci": [round(x, 1) for x in wilson(r, n)],
    }


# -------------------------------------------------------------- gate replay

CANDIDATE_RULES = {
    "current_gate": lambda s: bool(s["gate_refusal"]),
    "no_first_name": lambda s: not (s["first_name"] or "").strip(),
    "role_local_extended": lambda s: local_part(s["email"]) in ROLE_LOCALS | {
        "bot", "dev", "media", "pilot", "questions", "enterprise", "ops",
        "ask", "feedback", "growth", "marketing", "partnerships", "community",
    } or "+" in local_part(s["email"]),
    "any_catchall": lambda s: s["verify_status"] == "catchall",
    "any_unverified": lambda s: s["verify_status"] == "unverified",
    "guess_provenance": lambda s: s["provenance"] == "guess",
}


def gate_replay(sends: list[dict]) -> dict:
    """For each rule: of past sends, how many bounces it would have stopped
    and how many replies and clean sends it would have cost."""
    out = {}
    bounced = [s for s in sends if s["bounced"]]
    replied = [s for s in sends if s["replied"]]
    clean = [s for s in sends if not s["bounced"]]
    for name, rule in CANDIDATE_RULES.items():
        hits = [s for s in sends if rule(s)]
        out[name] = {
            "would_refuse": len(hits),
            "bounces_stopped": sum(1 for s in bounced if rule(s)),
            "bounces_total": len(bounced),
            "replies_lost": sum(1 for s in replied if rule(s)),
            "replies_total": len(replied),
            "clean_sends_lost": sum(1 for s in clean if rule(s)),
            "clean_total": len(clean),
        }
    return out


# -------------------------------------------------------------- ramp replay

def recent_bounce_pct(conn: sqlite3.Connection, before_local_date: str,
                      window: int = 100, recurrable_only: bool = False) -> float:
    """Reimplementation of Database.recent_bounce_pct for replay, with one
    optional variant: count only bounced contacts the current gates would
    still mail (their class of bounce can recur). Gate-refused classes
    cannot produce new bounces, so holding the cap for them protects
    nothing; the variant quantifies that difference.
    """
    rows = conn.execute(
        """SELECT c.email, c.status, c.verify_status, c.provenance
           FROM (SELECT contact_id FROM sends
                 WHERE status = 'sent' AND local_date < ?
                 ORDER BY id DESC LIMIT ?) s
           JOIN contacts c ON c.id = s.contact_id""",
        (before_local_date, window)).fetchall()
    if not rows:
        return 0.0
    bounced = 0
    for row in rows:
        if row["status"] != "bounced":
            continue
        if recurrable_only and not sendable_address_class(
                row["email"], row["verify_status"], row["provenance"]):
            continue
        bounced += 1
    return 100.0 * bounced / len(rows)


def ramp_replay(conn: sqlite3.Connection, start: int = 8, step: int = 2,
                limit: int = 25, max_pct: float = 5.0) -> list[dict]:
    """Day by day: both bounce series, and the cap under the live rule.

    `held` and `cap` follow the recurrable series, which is what the ramp
    reads now. The raw series stays in the table so the days where the two
    rules disagree are visible; historical `sent` values were of course
    produced by whatever rule was deployed at the time. The growth limiter
    (daily_cap's recent_daily_max) is not modelled here; it binds only on
    the first days after a release.
    """
    days = [r["local_date"] for r in conn.execute(
        "SELECT DISTINCT local_date FROM sends WHERE status='sent' ORDER BY 1")]
    series = []
    for day in days:
        active = conn.execute(
            """SELECT COUNT(DISTINCT local_date) FROM sends
               WHERE status='sent' AND local_date < ?""", (day,)).fetchone()[0]
        pct = recent_bounce_pct(conn, day)
        pct_recur = recent_bounce_pct(conn, day, recurrable_only=True)
        earned = start + step * active
        cap = max(0, min(limit, earned))
        if pct_recur > max_pct:
            cap = max(0, min(cap, start))
        sent = conn.execute(
            "SELECT COUNT(*) FROM sends WHERE status='sent' AND local_date=?",
            (day,)).fetchone()[0]
        series.append({
            "date": day, "bounce_pct": round(pct, 1),
            "bounce_pct_recurrable": round(pct_recur, 1),
            "cap": cap, "held": pct_recur > max_pct, "sent": sent,
        })
    return series


# ------------------------------------------------------------- queue supply

def queue_supply(conn: sqlite3.Connection) -> dict:
    out: dict = {}
    out["contacts_by_status"] = {
        r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM contacts GROUP BY status")}
    out["no_contacts_by_reason"] = {
        r["skip_reason"] or "(blank)": r["n"] for r in conn.execute(
            """SELECT skip_reason, COUNT(*) AS n FROM companies
               WHERE status='no_contacts' GROUP BY skip_reason ORDER BY n DESC""")}

    eligible = {"pending": 0, "queued": 0}
    refused: dict[str, int] = {}
    for row in conn.execute(
        """SELECT c.email, c.verify_status, c.provenance, c.status
           FROM contacts c
           LEFT JOIN suppression su ON su.email = lower(c.email)
           WHERE c.status IN ('pending', 'queued') AND su.email IS NULL"""):
        why = gate_would_refuse(row["email"], row["verify_status"], row["provenance"])
        if why:
            refused[why] = refused.get(why, 0) + 1
        else:
            eligible[row["status"]] += 1
    out["eligible_now"] = eligible
    out["pending_or_queued_refused"] = refused

    out["queued_drafts_gate_refused"] = sum(
        1 for r in conn.execute(
            """SELECT c.email, c.verify_status, c.provenance
               FROM drafts d JOIN contacts c ON c.id = d.contact_id
               WHERE d.status = 'queued'""")
        if gate_would_refuse(r["email"], r["verify_status"], r["provenance"]))

    out["followup_stock"] = conn.execute(
        """SELECT COUNT(*) FROM contacts c
           LEFT JOIN suppression su ON su.email = lower(c.email)
           WHERE c.status = 'sent' AND su.email IS NULL
             AND (SELECT COUNT(*) FROM sends s
                  WHERE s.contact_id = c.id AND s.status='sent') = 1"""
    ).fetchone()[0]

    total_eligible = sum(eligible.values())
    out["runway_days"] = {
        "at_8_per_day": round(total_eligible / 8, 1),
        "at_25_per_day": round(total_eligible / 25, 1),
    }
    return out


# ------------------------------------------------------------------ report

def render_markdown(result: dict) -> str:
    lines = ["# Mailbot scorecard", ""]
    lines.append(f"Snapshot: {result['db']}  ")
    lines.append(f"Generated: {result['generated_at']}  ")
    lines.append(f"Training period: sends before {result['split']}"
                 f" ({result['train_sends']} sends); holdout"
                 f" {'INCLUDED' if result['holdout_used'] else 'untouched'}"
                 f" ({result['holdout_sends']} sends).")
    lines.append("")
    lines.append("Caveat: verify_status and provenance are today's values; "
                 "send-time values are not reconstructable (reprobe rewrites, "
                 "provenance backfilled 2026-11-01).")

    lines.append("\n## Label audit\n")
    for key, value in result["label_audit"].items():
        lines.append(f"- {key}: {value}")

    lines.append("\n## Rates by segment (training period)\n")
    for name, rows in result["segments"].items():
        lines.append(f"### {name}\n")
        lines.append("| segment | sends | bounced | bounce% (95% CI) | replied | reply% (95% CI) |")
        lines.append("|---|---|---|---|---|---|")
        for row in rows:
            lines.append(
                f"| {row['segment']} | {row['sends']} | {row['bounced']} "
                f"| {row['bounce_pct']} ({row['bounce_ci'][0]}-{row['bounce_ci'][1]}) "
                f"| {row['replied']} | {row['reply_pct']} "
                f"({row['reply_ci'][0]}-{row['reply_ci'][1]}) |")
        lines.append("")

    lines.append("## Per-company rates (training period)\n")
    lines.append(f"- {json.dumps(result['company_rates'])}")

    lines.append("\n## Gate replay: would-this-bounce (all sends)\n")
    lines.append("| rule | refuses | bounces stopped | replies lost | clean sends lost |")
    lines.append("|---|---|---|---|---|")
    for name, row in result["gate_replay"].items():
        lines.append(
            f"| {name} | {row['would_refuse']} "
            f"| {row['bounces_stopped']}/{row['bounces_total']} "
            f"| {row['replies_lost']}/{row['replies_total']} "
            f"| {row['clean_sends_lost']}/{row['clean_total']} |")

    lines.append("\n## Ramp replay (last 15 send days)\n")
    lines.append("| date | bounce% | recurrable% | cap | held | sent |")
    lines.append("|---|---|---|---|---|---|")
    for row in result["ramp"][-15:]:
        lines.append(f"| {row['date']} | {row['bounce_pct']} "
                     f"| {row['bounce_pct_recurrable']} | {row['cap']} "
                     f"| {'yes' if row['held'] else ''} | {row['sent']} |")

    lines.append("\n## Queue and supply\n")
    lines.append("```json")
    lines.append(json.dumps(result["queue"], indent=2))
    lines.append("```")
    return "\n".join(lines) + "\n"


def run(db_path: str, out_dir: str, split: str = DEFAULT_SPLIT,
        holdout: bool = False) -> dict:
    conn = open_ro(db_path)
    try:
        sends = send_table(conn)
        train = [s for s in sends if s["local_date"] < split]
        hold = [s for s in sends if s["local_date"] >= split]
        scored = sends if holdout else train
        from datetime import datetime, timezone
        result = {
            "db": str(db_path),
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "split": split,
            "holdout_used": holdout,
            "train_sends": len(train),
            "holdout_sends": len(hold),
            "label_audit": label_audit(conn),
            "segments": segment_rates(scored),
            "company_rates": company_rates(scored),
            "gate_replay": gate_replay(sends),
            "ramp": ramp_replay(conn),
            "queue": queue_supply(conn),
        }
    finally:
        conn.close()

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "scorecard.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    (out / "scorecard.md").write_text(render_markdown(result), encoding="utf-8")
    if holdout:
        with (out / "holdout_access.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{result['generated_at']} holdout read\n")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--split", default=DEFAULT_SPLIT)
    parser.add_argument("--holdout", action="store_true",
                        help="include holdout sends in segment tables (logged)")
    args = parser.parse_args(argv)
    result = run(args.db, args.out, args.split, args.holdout)
    print(f"scorecard written to {args.out}: {result['train_sends']} train sends, "
          f"{result['holdout_sends']} holdout sends")
    return 0


if __name__ == "__main__":
    sys.exit(main())
