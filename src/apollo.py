"""Import an Apollo contacts export, on the same gates as every other source.

Apollo's own "Verified" is not the same claim as ours. Its export marks an
address Verified on a catch-all domain too (the same file says so in its
"Primary Email Catch-all Status" column), and on a catch-all domain no probe
can confirm anything. So every address is probed here as usual and our own
verdict decides:

  verified     a normal contact any lane may send, provenance "apollo"
  catch-all    kept only when Apollo itself says Verified and VOLUME_GUESSES
               is on, and then reserved for the volume lane, never main
  anything     skipped: rejected, no mail host, or no SMTP answer (import
  else         the file again later and those are tried afresh)

Company size, a person's name and the one-per-company cap apply exactly as
they do to scraped contacts. Not scheduled: run it by hand with a fresh
export (mailbot import-apollo export.csv).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .models import (
    CATCHALL, Company, Contact, ENRICHED, PENDING, PROV_APOLLO, SRC_INFERRED,
    UNVERIFIED, VERIFIED, VOLUME_LANE,
)
from .verifier import domain_of, is_personal_mailbox

# A catch-all address Apollo vouches for, ranked under every proven one.
APOLLO_CATCHALL_CONFIDENCE = 45


@dataclass
class ImportReport:
    rows: int = 0
    added: int = 0
    reserved: int = 0
    lines: list[tuple[str, str, str]] = field(default_factory=list)

    def skip(self, email: str, why: str) -> None:
        self.lines.append((email or "-", "skip", why))


def _cell(row: dict, *names: str) -> str:
    for name in names:
        value = (row.get(name) or "").strip()
        if value:
            return value
    return ""


def site_domain(website: str) -> str:
    """apollo's Website column, "https://www.x.ai" -> "x.ai"."""
    website = (website or "").strip()
    if not website:
        return ""
    host = urlparse(website if "://" in website else f"https://{website}").hostname or ""
    host = host.lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def same_company(email_domain: str, website_domain: str) -> bool:
    """The address belongs to the company whose site the row names."""
    if not email_domain or not website_domain:
        return False
    return (email_domain == website_domain
            or email_domain.endswith("." + website_domain)
            or website_domain.endswith("." + email_domain))


def _employees(raw: str) -> int | None:
    try:
        return int(raw.replace(",", "")) if raw else None
    except ValueError:
        return None


def read_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def import_export(path: Path, db, verifier, settings, any_size: bool = False,
                  dry_run: bool = False, limit: int = 0) -> ImportReport:
    report = ImportReport()
    rows = read_rows(path)
    if limit:
        rows = rows[:limit]
    report.rows = len(rows)
    cap = settings.max_contacts_per_company
    guesses_on = bool(getattr(settings, "volume_guesses", False))

    for row in rows:
        email = _cell(row, "Email").lower()
        first = _cell(row, "First Name")
        if not email:
            report.skip("", f"no email for {first or 'a row'} "
                            f"({_cell(row, 'Email Status') or 'blank'})")
            continue
        if not first:
            report.skip(email, "no first name to write to")
            continue
        if not is_personal_mailbox(email):
            report.skip(email, "shared or blocked inbox")
            continue
        website = _cell(row, "Website")
        domain = site_domain(website)
        if not same_company(domain_of(email), domain):
            report.skip(email, f"address is not on the company site's domain ({domain or 'none'})")
            continue
        employees = _employees(_cell(row, "# Employees"))
        if (not any_size and employees is not None
                and employees > settings.max_employees):
            report.skip(email, f"{employees} employees, over MAX_EMPLOYEES "
                               f"{settings.max_employees}")
            continue
        allowed, why = db.can_send_to(email)
        if not allowed or db.contact_exists(email):
            report.skip(email, why or "already known")
            continue

        verdict = verifier.verify(email, source=SRC_INFERRED)
        lane = ""
        if verdict.status == VERIFIED:
            confidence = verdict.confidence
        elif verdict.status == CATCHALL:
            apollo_says = _cell(row, "Email Status").lower()
            if not guesses_on or apollo_says != "verified":
                report.skip(email, "catch-all domain, nothing to prove it with"
                            + ("" if guesses_on else " (VOLUME_GUESSES is off)"))
                continue
            lane, confidence = VOLUME_LANE, APOLLO_CATCHALL_CONFIDENCE
        else:
            note = ("no SMTP answer, import again later"
                    if verdict.status == UNVERIFIED else verdict.detail)
            report.skip(email, f"{verdict.status}: {note}")
            continue

        location = ", ".join(p for p in (
            _cell(row, "Company City", "City"), _cell(row, "Company State", "State"),
            _cell(row, "Company Country", "Country")) if p)
        keywords = _cell(row, "Keywords")
        company = Company(
            name=_cell(row, "Company Name for Emails", "Company Name") or domain,
            domain=domain, website=website, location=location,
            industry=_cell(row, "Industry"),
            one_liner=(keywords or _cell(row, "Industry"))[:180],
            description=keywords[:1200], employees=employees,
            source="apollo", status=ENRICHED,
        )
        if dry_run:
            report.added += 1
            report.reserved += bool(lane)
            report.lines.append((email, f"would add{' for ' + lane if lane else ''}",
                                 f"{verdict.status}, {company.name}"))
            continue
        company_id = db.upsert_company(company)
        open_, why = db.company_slot(company_id, cap)
        if not open_:
            report.skip(email, why)
            continue
        added = db.upsert_contact(Contact(
            email=email, company_id=company_id, first_name=first,
            last_name=_cell(row, "Last Name"), role=_cell(row, "Title"),
            linkedin=_cell(row, "Person Linkedin Url"), confidence=confidence,
            source=SRC_INFERRED, verify_status=verdict.status,
            verify_detail=f"from Apollo ({_cell(row, 'Email Status') or '?'}): "
                          f"{verdict.detail}",
            status=PENDING, provenance=PROV_APOLLO, lane=lane,
        ))
        if added is None:
            report.skip(email, "duplicate")
            continue
        report.added += 1
        report.reserved += bool(lane)
        report.lines.append((email, f"added{' for ' + lane if lane else ''}",
                             f"{verdict.status}, {company.name}"))
    return report
