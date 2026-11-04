"""The outreach pipeline: one class per stage of the funnel.

Split from main.py so the command line layer stays thin and this file stays
readable. Stages are deliberately independent so each can be run and inspected
on its own before the next.
"""

from __future__ import annotations

import contextlib
import imaplib
import math
import smtplib
import time
from collections import Counter
from pathlib import Path

from .config import Settings
from .database import Database
from .emailer import (
    WindowState, draft_defect, message_for_draft, resume_link_line,
    strip_re_prefix, with_resume_link,
    build_message, in_send_window, load_template, local_date,
    make_backend, pace_delay, unedited_markers,
    validate_template, window_state,
)
from .lanes import guess_hold, is_guess, spread
from .mirror import GmailDrafts
from .finder import discover as discover_companies, enrich_company
from .gh_discovery import discover as discover_orgs
from .github_source import GitHubClient
from .hn_source import fetch_posts, fetch_recent_posts
from .llm import Personalizer
from .models import (
    CATCHALL, Company, Contact, ENRICHED, MAIN_LANE, NO_CONTACTS, PENDING,
    SKIP_CATCHALL_UNPROVABLE, SRC_SCRAPED, PROV_HN_POST, SKIP_NOT_VERIFIED,
    TRUSTED_PROVENANCE, deliverable, lane_deliverable, volume_tier,
)
from . import priority as priority_list
from .priority import from_settings as priority_set
from .scraper import Fetcher
from .vc_source import SiteRanks, discover as discover_portfolios
from .reporting import BAR, _print_table, build_digest, print_summary
from .verifier import (
    Verifier, is_never_send, is_role_account,
    parse_relay,
)

_PRIORITY_HEADER = """\
# Companies to treat as high priority: enriched, drafted and sent before
# anything else, and exempt from the team-size and company-age gates that the
# rest of the funnel applies. See src/priority.py for what is and is not
# relaxed. Sending rules are not relaxed.
#
# One company per line. Add "= domain.com" to pin the domain; leave it off and
#   mailbot priority --resolve
# will work it out and write it back here, skipping any it cannot confirm.
# Lines starting with # are ignored, so a name can be parked without deleting.
"""


def _reserved_row(row) -> str:
    """The lane a draft row is reserved for, "" when any lane may send it."""
    return (row["contact_lane"] if "contact_lane" in row.keys() else "") or ""


class Pipeline:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.db_path, getattr(settings, "lane", MAIN_LANE))
        self.fetcher = Fetcher(
            settings.scrape_user_agent,
            per_minute=settings.http_rate_limit_per_min,
            max_fetches=settings.http_max_fetches_per_run,
        )
        helo = settings.from_email.split("@")[-1] if settings.from_email else "example.com"
        self.verifier = Verifier(
            db=self.db,
            helo_domain=helo,
            mail_from=settings.from_email or "verify@example.com",
            relay=parse_relay(settings.smtp_probe_relay),
        )
        self.personalizer = Personalizer(
            settings.groq_api_key, settings.groq_model,
            max_calls=settings.groq_max_calls_per_run,
        )
        self.github = GitHubClient(
            settings.github_token, max_calls=settings.github_max_calls_per_run
        )

    def close(self) -> None:
        self.db.close()

    @property
    def lane(self) -> str:
        return getattr(self.db, "lane", MAIN_LANE) or MAIN_LANE

    def _tag(self, stage: str) -> str:
        """The log prefix: "send" on main, "send[volume]" on another lane, so
        two accounts writing one log stay apart."""
        return stage if self.lane == MAIN_LANE else f"{stage}[{self.lane}]"

    # ---------------- stages ----------------

    def discover(self, limit: int) -> int:
        priority = priority_set(self.settings)
        found, stats = discover_companies(self.fetcher, self.settings, self.db,
                                          limit=limit, priority=priority)
        for company in found:
            if priority.has(name=company.name, domain=company.domain):
                company.priority = 1
            self.db.upsert_company(company)
        print(f"discovery: {stats.qualified} qualified of {stats.considered} scanned, "
              f"{stats.already_known} already known, {len(found)} added")
        if found:
            _print_table(
                [(c.name, c.domain, c.location[:20], c.employees, c.batch,
                  "hiring" if c.is_hiring else "") for c in found],
                ("company", "domain", "location", "team", "batch", ""),
            )
        return len(found)

    def discover_github(self, limit: int = 40) -> int:
        """Companies from GitHub org search, alongside the YC directory.

        Runs on the same funnel as everything else: rows land with status=new
        and the existing enrich stage takes them from there.
        """
        if not self.settings.github_token:
            print("gh: set GITHUB_TOKEN, the search endpoint needs one")
            return 0
        found, stats = discover_orgs(self.github, self.settings, self.db, limit=limit)
        for company in found:
            self.db.upsert_company(company)
        print(f"gh: {stats.added} added of {stats.scanned} orgs scanned "
              f"({stats.already_known} already known, {stats.rejected} no usable "
              f"domain, {self.github.budget})")
        if found:
            _print_table(
                [(c.name, c.domain, c.location[:20], c.github_org,
                  c.founded_year or "") for c in found],
                ("company", "domain", "location", "org", "since"),
            )
        return len(found)

    def discover_vc(self, limit: int = 30, pages: int = 8) -> int:
        """Companies from US and Canadian fund portfolio pages.

        Same funnel as the other sources: rows land with status=new, source=vc,
        and enrich takes them from there.
        """
        ranks = SiteRanks(self.settings.db_path.parent / "cache" / "tranco-top.txt",
                          self.settings.vc_max_site_rank)
        found, stats = discover_portfolios(self.fetcher, self.settings, self.db,
                                           ranks, limit=limit, pages_per_run=pages)
        for company in found:
            self.db.upsert_company(company)
        print(f"vc: {stats.added} added from {stats.pages} portfolio page(s) "
              f"({stats.pages_failed} failed), {stats.candidates} listed, "
              f"{stats.already_known} already known, {stats.too_big} big companies, "
              f"{stats.dead} sites down, {stats.moved} moved to another domain "
              f"({self.fetcher.budget})")
        if found:
            _print_table(
                [(c.name, c.domain, c.industry[:24], c.one_liner[:40]) for c in found],
                ("company", "domain", "via", "one-liner"),
            )
        return len(found)

    def priority(self, resolve: bool = False, limit: int = 0) -> int:
        """Seed the named companies and put them at the front of the queue.

        Three things happen, in this order:

          flag     companies already in the database whose name or domain is
                   on the list, so a name that arrived through discovery gets
                   its place without being inserted twice
          resolve  work out the domain for any entry that has none, and write
                   it back to the file so the next run costs nothing
          seed     insert the rest as status=new, which is all enrich needs to
                   pick them up on its next pass

        Flags are cleared first, so deleting a name from the file actually
        demotes the company rather than leaving it permanently ahead.
        """
        entries = priority_list.load(self.settings.priority_path)
        if not entries:
            print(f"priority: nothing in {self.settings.priority_path}")
            return 0

        known = priority_list.PrioritySet(entries)
        cleared = self.db.clear_priorities()
        flagged = 0
        by_domain: dict[str, int] = {}
        for row in self.db.all_companies():
            if known.has(name=row["name"], domain=row["domain"]):
                self.db.mark_priority(row["id"])
                flagged += 1
            by_domain[(row["domain"] or "").lower()] = row["id"]

        rows: list[tuple] = []
        resolved = seeded = 0
        pending = [e for e in entries if not e.domain]
        if resolve and pending:
            print(f"priority: resolving {len(pending)} domain(s), "
                  "this makes a DNS lookup per candidate and a fetch per "
                  "domain that takes mail")
        for entry in entries:
            if not entry.domain:
                if not resolve:
                    rows.append((entry.name, "-", "no domain",
                                 "run with --resolve"))
                    continue
                if limit and resolved >= limit:
                    rows.append((entry.name, "-", "not tried",
                                 f"--limit {limit} reached"))
                    continue
                entry.domain, entry.note = priority_list.resolve(
                    entry.name, self.fetcher, self.verifier)
                resolved += 1
                if not entry.domain:
                    rows.append((entry.name, "-", "unresolved", entry.note))
                    continue
            existing = by_domain.get(entry.domain.lower())
            if existing:
                self.db.mark_priority(existing)
                rows.append((entry.name, entry.domain, "known", "flagged"))
                continue
            company_id = self.db.upsert_company(Company(
                name=entry.name,
                domain=entry.domain,
                website=f"https://{entry.domain}",
                source="priority",
                priority=1,
            ))
            by_domain[entry.domain.lower()] = company_id
            seeded += 1
            rows.append((entry.name, entry.domain, "seeded", "queued for enrich"))

        if resolve:
            # Written back whatever happened, so a resolved domain is never
            # looked up twice and an unresolved one carries its reason.
            Path(self.settings.priority_path).write_text(
                priority_list.render(entries, _PRIORITY_HEADER), encoding="utf-8")

        print(f"priority: {len(entries)} listed, {seeded} seeded, "
              f"{flagged} already known and flagged, {cleared} flag(s) cleared "
              f"first, {sum(1 for e in entries if not e.domain)} still without "
              "a domain")
        _print_table(rows, ("company", "domain", "state", "detail"))
        return seeded

    def enrich(self, limit: int) -> int:
        companies = self.db.companies_by_status("new", limit=limit)
        if not companies:
            print("enrich: no companies with status=new")
            return 0
        added = 0
        rows = []
        for company in companies:
            result = enrich_company(company, self.fetcher, self.verifier,
                                    self.settings, github=self.github)
            if not result.contacts:
                self.db.set_company_status(company["id"], NO_CONTACTS, result.note)
                rows.append((company["name"], "-", "-", 0, result.note[:34]))
                continue
            kept = 0
            note = result.note
            for contact in result.contacts:
                # Only what the send gate accepts is worth storing: a verified
                # mailbox, or a catch-all one from a trusted source, or a
                # guess reserved for the volume lane, which only it may send.
                reserved = getattr(contact, "lane", "")
                if not lane_deliverable(contact.verify_status, contact.provenance,
                                        reserved, reserved or MAIN_LANE):
                    rows.append((company["name"], contact.email, "skip", 0,
                                 f"{contact.verify_status}, {contact.provenance}"))
                    note = note or SKIP_NOT_VERIFIED
                    continue
                allowed, why = self.db.can_send_to(contact.email, reserved)
                if not allowed:
                    rows.append((company["name"], contact.email, "skip", 0, why))
                    continue
                if self.db.upsert_contact(contact) is None:
                    rows.append((company["name"], contact.email, "skip", 0, "duplicate"))
                    continue
                kept += 1
                added += 1
                rows.append((company["name"], contact.email,
                             contact.verify_status, contact.confidence,
                             (f"{reserved} lane, " if reserved else "")
                             + (contact.role[:34] or contact.source)))
            self.db.set_company_status(
                company["id"], ENRICHED if kept else NO_CONTACTS,
                result.note if kept else note,
            )
        print(f"enrich: {added} contacts added from {len(companies)} companies "
              f"({self.fetcher.budget}, {self.github.budget})")
        _print_table(rows, ("company", "email", "verify", "conf", "role/note"))
        return added

    def import_hn(self, limit: int = 40, months: int = 1) -> int:
        """Pull contacts from the monthly Hacker News hiring threads.

        These arrive already enriched: the poster published their own address,
        so there is nothing to infer and nothing to guess. They still go through
        verification and the same duplicate gate as everything else.

        `months` reads that many threads back. The addresses do not rot and the
        companies are mostly still hiring, so a backfill is close to free volume.
        """
        settings = self.settings
        if months > 1:
            posts = fetch_recent_posts(settings.target_locations, months=months)
        else:
            posts = fetch_posts(settings.target_locations)
        if not posts:
            print("hn: no qualifying posts in the threads read")
            return 0
        added, rows = 0, []
        cap = settings.max_contacts_per_company
        for post in posts[:limit]:
            if not post.company or not post.domain:
                continue
            company_id = self.db.upsert_company(Company(
                name=post.company, domain=post.domain, website=post.website,
                location=post.location, one_liner=post.text[:180].replace("\n", " "),
                description=post.text[:1200], is_hiring=True, source="hn",
                status=ENRICHED,
            ))
            open_, why = self.db.company_slot(company_id, cap)
            if not open_:
                rows.append((post.company, "-", "skip", 0, why))
                continue
            slots = cap - self.db.company_contacted_count(company_id)
            # Named addresses first: shared inboxes are refused at the send
            # gate, so verifying them only spends probes.
            usable = []
            for address in sorted(post.emails, key=is_role_account):
                if len(usable) >= slots:
                    break
                if is_never_send(address) or address.split("@")[1] != post.domain:
                    continue
                verdict = self.verifier.verify(address, source=SRC_SCRAPED)
                if deliverable(verdict.status, PROV_HN_POST):
                    usable.append((address, verdict))
            if not usable:
                rows.append((post.company, "-", "-", 0, "no usable address"))
                continue
            for address, verdict in usable:
                allowed, why = self.db.can_send_to(address)
                if not allowed:
                    rows.append((post.company, address, "skip", 0, why))
                    continue
                contact_id = self.db.upsert_contact(Contact(
                    email=address, company_id=company_id, source=SRC_SCRAPED,
                    role="posted the hiring ad", verify_status=verdict.status,
                    verify_detail=f"published on HN: {verdict.detail}",
                    confidence=verdict.confidence, status=PENDING,
                    provenance=PROV_HN_POST,
                ))
                if contact_id is None:
                    rows.append((post.company, address, "skip", 0, "duplicate"))
                    continue
                added += 1
                rows.append((post.company, address, verdict.status,
                             verdict.confidence, post.location))
        print(f"hn: {added} contacts added from {len(posts)} qualifying posts")
        _print_table(rows, ("company", "email", "verify", "conf", "location"))
        return added

    def reopen_catchall(self, limit: int) -> int:
        """Put companies parked as "catch-all, guesses unprovable" back in
        the enrich queue, a few a day, so the volume lane's guess tier has
        people to write to. Off unless VOLUME_GUESSES is on, since without
        it enrich would only park them again. Few at a time because each one
        re-enriched costs fetches that new companies also need."""
        if not getattr(self.settings, "volume_guesses", False):
            print("reopen-catchall: VOLUME_GUESSES is off, nothing to do")
            return 0
        parked = self.db.companies_parked_on(SKIP_CATCHALL_UNPROVABLE)
        for row in parked[:max(0, limit)]:
            self.db.reopen_company(row["id"])
        reopened = min(len(parked), max(0, limit))
        print(f"reopen-catchall: {reopened} of {len(parked)} parked catch-all "
              "companies back in the enrich queue")
        return reopened

    def import_apollo(self, path: Path, any_size: bool = False,
                      dry_run: bool = False, limit: int = 0) -> int:
        """Load an Apollo contacts export. See apollo.py for the rules."""
        from .apollo import import_export
        report = import_export(Path(path), self.db, self.verifier, self.settings,
                               any_size=any_size, dry_run=dry_run, limit=limit)
        verb = "would add" if dry_run else "added"
        print(f"apollo: {verb} {report.added} contact(s) from {report.rows} row(s), "
              f"{report.reserved} of them reserved for the volume lane")
        _print_table(report.lines, ("email", "state", "detail"))
        return report.added

    def queue(self, limit: int, target: int = 0) -> int:
        """Render drafts. With a target, only enough to reach that depth.

        Drafting runs all day rather than once in the morning, so most runs
        find the queue already deep enough and should cost nothing. Rendering
        calls the LLM once per draft, so "nothing to do" has to be decided
        before that and not after.
        """
        # Two depths, kept apart: drafts any lane may send, and the volume
        # lane's reserved guesses. One shared target let guesses fill the
        # queue and stop verified people being drafted at all.
        guesses_on = bool(getattr(self.settings, "volume_guesses", False))
        room = {False: limit, True: limit if guesses_on else 0}
        if target:
            have_open = self.db.queued_draft_count(reserved=False)
            have_held = self.db.queued_draft_count(reserved=True) if guesses_on else 0
            room = {False: max(0, target - have_open),
                    True: max(0, target - have_held) if guesses_on else 0}
            if not any(room.values()):
                print(f"queue: {have_open + have_held} already waiting, target "
                      f"{target}, nothing to render")
                return 0
            limit = min(limit, room[False] + room[True])
        template = load_template(self.settings.template_path)
        validate_template(template)
        holes = unedited_markers(f"{template.subject}\n{template.body}")
        if holes:
            print(f"warning: template still has {len(holes)} unedited placeholder(s): "
                  f"{', '.join(holes[:3])}")
            print("         drafts will queue for preview but send() will refuse them")
        # The hosted link wins when one is set, since a PDF from a stranger
        # trips spam filters. The PDF is attached only when there is no link,
        # never both: a message carrying both shows two resume chips.
        attachments = []
        resume_link = ""
        if self.settings.resume_link:
            resume_link = resume_link_line(self.settings.resume_link)
        elif self.settings.resume_path.exists():
            attachments.append(str(self.settings.resume_path))
        else:
            print(f"warning: resume not found at {self.settings.resume_path} "
                  "and no RESUME_LINK is set; queuing without either, "
                  "send() will refuse these drafts")

        # Every pending contact, not a slice: with the open queue full, a
        # slice of the best-ranked rows held no reserved ones at all.
        contacts = self.db.contacts_by_status(PENDING, limit=-1)
        queued = 0
        rows = []
        for row in contacts:
            if queued >= limit:
                break
            reserved = (row["lane"] if "lane" in row.keys() else "") or ""
            if room[bool(reserved)] <= 0:
                continue
            # A reserved guess sits under MIN_CONFIDENCE by design, and is
            # judged at send time by the volume lane's own brake instead.
            if not reserved and row["confidence"] < self.settings.min_confidence:
                rows.append((row["email"], "skip",
                             f"confidence {row['confidence']} < "
                             f"{self.settings.min_confidence}"))
                continue
            allowed, why = self.db.can_send_to(row["email"], reserved)
            if not allowed:
                rows.append((row["email"], "skip", why))
                continue
            if row["company_id"]:
                open_, why = self.db.company_slot(
                    row["company_id"], self.settings.max_contacts_per_company)
                if not open_:
                    rows.append((row["email"], "skip", why))
                    continue
            # Only a name we actually found for this person. Reading one off the
            # address sent "Dear Git," "Dear Future," and "Dear Pilot," and
            # nameless addresses drew 1 reply in 190 against far better for
            # named people. Checked before personalising so a skip costs no
            # LLM call.
            first = (row["first_name"] or "").strip()
            if not first:
                rows.append((row["email"], "skip", "no person's name"))
                continue
            note = self.personalizer.line_for(
                row["company_name"] or "", row["one_liner"] or "",
                row["description"] or "", row["role"] or "",
            )
            if not note:
                # The ask lives at the end of the personal line, so an email
                # without one still needs it on its own.
                company = row["company_name"] or "your company"
                note = f"would love to discuss opportunities at {company}."
            context = {
                "first_name": first,
                "full_name": " ".join(
                    p for p in (row["first_name"], row["last_name"]) if p
                ),
                "company": row["company_name"] or "",
                "role": row["role"] or "",
                "location": row["location"] or "",
                "batch": row["batch"] or "",
                "one_liner": row["one_liner"] or "",
                "personal_note": note,
                "sender_name": self.settings.from_name,
                "sender_email": self.settings.from_email,
                "unsubscribe": self.settings.unsubscribe_mailto,
                "resume_link": resume_link,
            }
            subject, body = template.render(context)
            if resume_link:
                # Covers a template that has no {resume_link} placeholder.
                body = with_resume_link(body, self.settings.resume_link)
            self.db.queue_draft(row["id"], subject, body, attachments)
            queued += 1
            room[bool(reserved)] -= 1
            rows.append((row["email"], f"queued for {reserved}" if reserved
                         else "queued", subject[:40]))
        print(f"queue: {queued} drafts ready ({self.personalizer.budget})")
        failures = getattr(self.personalizer, "failures", None)
        if failures:
            # A failed call only drops the optional personal line, so nothing
            # else would ever show that the model had stopped answering.
            print(f"queue: {sum(failures.values())} groq call(s) failed, so "
                  "those drafts carry the generic line: " + ", ".join(
                      f"{n}x {why}" for why, n in failures.most_common(3)))
        _print_table(rows, ("email", "status", "detail"))
        return queued

    def _split_incomplete(self, drafts: list) -> tuple[list, list[tuple[str, str]]]:
        """Separate drafts that are safe to send from ones that are not finished.

        The rule itself lives in emailer.draft_defect, because the Gmail
        mirror has to apply exactly the same one before a copy lands somewhere
        a human can send it by hand.
        """
        ready, blocked = [], []
        for draft in drafts:
            why = draft_defect(draft, self.settings)
            if why:
                blocked.append((draft["email"], why))
            else:
                ready.append(draft)
        return ready, blocked

    def _todays_cap(self, today: str) -> tuple[int, str]:
        """Today's send cap, after the warmup ramp.

        Short-circuited when the ramp is off so the common case costs no
        queries. Both send and followup go through here, so the two share
        one cap rather than each getting a full allowance.
        """
        settings = self.settings
        if not getattr(settings, "send_ramp_enabled", False):
            cap, why = settings.daily_send_limit, "ramp off"
        else:
            cap, why = settings.daily_cap(
                self.db.active_send_days(today),
                self.db.recent_bounce_pct(before_local_date=today),
                self.db.max_daily_sends(before_local_date=today),
            )
        # On record so another lane can tell when this one is full, which is
        # when the open queue becomes that lane's overflow.
        record = getattr(self.db, "record_cap", None)
        if record is not None:
            record(today, cap)
        return cap, why

    def _catchall_rule(self, today: str):
        """Today's test for a catch-all address, and why: see
        Settings.catchall_provenance. None means no tightening applies.

        The volume lane's guess tier is left to its own brake, _guess_rule;
        these tiers are about addresses with real evidence behind them."""
        rule = getattr(self.settings, "catchall_provenance", None)
        if rule is None:
            return None, ""
        allowed, why = rule(self.db.recent_bounce_pct(before_local_date=today))
        if allowed >= TRUSTED_PROVENANCE:
            return None, why

        def blocked(row) -> bool:
            return (row["verify_status"] == CATCHALL and row["provenance"] not in allowed
                    and not volume_tier(row["verify_status"], row["provenance"]))
        return blocked, why

    def _guess_rule(self, today: str):
        """The volume lane's brake on its guess tier: (test, why), or
        (None, "") when guesses may go. See lanes.guess_hold."""
        why = guess_hold(self.settings, self.db, today)
        return (is_guess if why else None), why

    def _window_states(self, rows: list, ignore_window: bool) -> list[tuple]:
        """Each row paired with where its recipient's send window stands."""
        settings = self.settings
        if ignore_window:
            anywhere = WindowState("open", "any", "any", 1)
            return [(row, anywhere) for row in rows]
        tick = getattr(settings, "send_tick_minutes", 15)

        def state(row) -> WindowState:
            # One clock for everyone unless windows follow the recipient.
            location = ""
            if settings.per_recipient_timezone and "company_location" in row.keys():
                location = row["company_location"] or ""
            return window_state(location, settings.send_timezone,
                                settings.send_window_start, settings.send_window_end,
                                tick)
        return [(row, state(row)) for row in rows]

    def _followup_pool(self, today: str, ignore_window: bool):
        """Every due bump, the ones this lane may send with their windows, and
        why the rest were refused, counted by reason.

        Refused rows stay due forever and every tick walks all of them, so
        they are tallied rather than printed one line each.
        """
        settings = self.settings
        # Every due row, not a slice: refused ones are never cleared and sort
        # oldest first, so a fixed slice filled with them and starved the rest.
        due = self.db.due_for_followup(
            settings.followup_after_days, settings.followup_max_total_sends,
            limit=None,
        )
        refused: Counter = Counter()
        catchall_blocked, catchall_why = self._catchall_rule(today)
        guess_blocked, guess_why = self._guess_rule(today)
        eligible = []
        for row in due:
            allowed, why = self.db.can_send_to_again(row["email"])
            if not allowed:
                refused[why] += 1
                continue
            if catchall_blocked and catchall_blocked(row):
                refused[catchall_why] += 1
                continue
            if guess_blocked and guess_blocked(row):
                refused[guess_why] += 1
                continue
            # A bump shares the daily cap with first emails, so it is spent
            # only on a known person. Nudging an unnamed address twice drew
            # opt-outs, never answers.
            if not (row["first_name"] or "").strip():
                refused["no person's name to follow up with"] += 1
                continue
            eligible.append(row)
        return due, self._window_states(eligible, ignore_window), refused

    def _followup_owed(self, today: str, cap: int, ignore_window: bool) -> int:
        """Bumps still to go out today from their share of the cap.

        The share is FOLLOWUP_SHARE_PCT of the cap, but never more than are
        actually due and reachable today, so a quiet follow-up day hands its
        slots back to first emails instead of leaving them unspent.
        """
        settings = self.settings
        share = getattr(settings, "followup_share_pct", 0)
        if not getattr(settings, "followup_enabled", False) or not share:
            return 0
        _due, states, _refused = self._followup_pool(today, ignore_window)
        reachable = sum(1 for _row, state in states if state.today)
        done = self.db.followup_count_on(today)
        return max(0, min(math.ceil(cap * share / 100), done + reachable) - done)

    def _first_emails_left(self, ignore_window: bool) -> int:
        """Queued first emails this lane could still reach today."""
        pool = self.db.queued_drafts(limit=None)
        return sum(1 for _row, state in self._window_states(pool, ignore_window)
                   if state.today)

    def followup(self, limit: int, ignore_window: bool = False) -> int:
        """Send one polite nudge to people who never answered.

        Deliberately capped by FOLLOWUP_MAX_TOTAL_SENDS (2 by default: the
        original plus one bump). Anyone who replied, bounced or asked to stop
        is excluded by the query, so this cannot turn into a drip campaign.
        """
        settings = self.settings
        tag = self._tag("followup")
        if not settings.followup_enabled:
            print(f"{tag}: disabled")
            return 0
        settings.require_for_send()
        template = load_template(settings.followup_template_path)
        validate_template(template)
        # The same rule the first email gets. validate_template only checks
        # that placeholder names are fillable; an unedited [BRACKET] is a hole
        # the author left, and send() blocks those for queued drafts. Nothing
        # blocked them here, so the follow-up was the one path an unfinished
        # template could still leave by.
        holes = unedited_markers(f"{template.subject}\n{template.body}")
        if holes:
            print(f"{tag}: refusing to send, {settings.followup_template_path} "
                  f"still has unedited placeholder(s): {', '.join(holes[:3])}")
            return 0

        # With per-recipient windows each nudge is gated on the recipient's own
        # morning further down. Without them there is a single clock, and it is
        # the one send() refuses against; until this check existed a follow-up
        # was the only way the bot could deliver at 3am.
        if not settings.per_recipient_timezone:
            ok, why = in_send_window(
                settings.send_timezone, settings.send_window_start,
                settings.send_window_end,
            )
            if not ok and not ignore_window:
                print(f"{tag}: refused, {why}")
                return 0

        today = local_date(settings.send_timezone)
        already = self.db.sent_count_on(today)
        cap, why_cap = self._todays_cap(today)
        remaining = max(0, cap - already)
        if remaining == 0:
            print(f"{tag}: daily cap reached ({already}/{cap}, {why_cap})")
            return 0

        due, states, refused = self._followup_pool(today, ignore_window)
        if not due:
            print(f"{tag}: nobody due")
            return 0

        # Same rule as the first email: the hosted link when one is set,
        # otherwise the PDF.
        attachments = ([str(settings.resume_path)]
                       if not settings.resume_link and settings.resume_path.exists()
                       else [])
        if not attachments and not settings.resume_link:
            print(f"{tag}: resume missing and no RESUME_LINK set, refusing to send")
            return 0

        # Bumps get their share of the cap, spread over the morning like
        # first emails. Once no first email is left to send today, nothing
        # else wants the rest of the cap, so they may use it.
        share = getattr(settings, "followup_share_pct", 0)
        allowance = remaining
        if share:
            done = self.db.followup_count_on(today)
            allowance = min(remaining, max(0, math.ceil(cap * share / 100) - done))
            if allowance < remaining and not self._first_emails_left(ignore_window):
                allowance = remaining
        picked = spread(states, min(limit, allowance))

        sent = 0
        # No login when there is nothing to send: most ticks are like that.
        backend = make_backend(settings) if picked else None
        try:
            with backend or contextlib.nullcontext():
                for index, row in enumerate(picked):
                    first = (row["first_name"] or "").strip()
                    subject, body = template.render({
                        "first_name": first,
                        "original_subject": strip_re_prefix(row["first_subject"] or ""),
                        "company": row["company_name"] or "",
                        "sender_name": settings.from_name,
                        "sender_email": settings.from_email,
                        "unsubscribe": settings.unsubscribe_mailto,
                        "resume_link": ("" if attachments
                                        else resume_link_line(settings.resume_link)),
                    })
                    if not attachments:
                        body = with_resume_link(body, settings.resume_link)
                    message = build_message(
                        subject, body, row["email"],
                        " ".join(p for p in (row["first_name"], row["last_name"]) if p),
                        settings.from_email, settings.from_name, settings.reply_to,
                        settings.unsubscribe_mailto, attachments,
                        in_reply_to=row["first_message_id"] or "",
                    )
                    if settings.dry_run:
                        print(f"{BAR}\nFOLLOWUP (dry run) to {row['email']}\n"
                              f"Subject: {subject}\n\n{body}")
                        continue
                    attempt = self.db.begin_send(row["id"], row["email"], subject, today)
                    result = backend.send(message)
                    self.db.finish_send(attempt, row["id"], result.message_id,
                                        result.backend, result.ok, result.error)
                    if result.ok:
                        sent += 1
                        print(f"  followed up {row['email']} ({row['company_name']})")
                    else:
                        print(f"  FAILED {row['email']}: {result.error}")
                    # Pacing separates messages; there is nothing to separate
                    # the last one from, and a tick that sends its single
                    # allowance should not then sit idle for twenty seconds.
                    if index < len(picked) - 1:
                        time.sleep(pace_delay(settings.send_delay_seconds))
        except smtplib.SMTPAuthenticationError:
            print(f"{tag}: SMTP login rejected, see send for details")
            return 0
        except (smtplib.SMTPException, OSError) as exc:
            print(f"{tag}: could not reach {settings.smtp_host}: {exc}")
            return 0
        if refused:
            print(f"{tag}: skipped " + ", ".join(
                f"{n} {why}" for why, n in refused.most_common()))
        print(f"{tag}: {sent} sent, {len(due)} were due")
        return sent

    def _space_by_company(self, drafts: list, today: str):
        """At most one first email per company per day, none once it has spoken.

        Two or three people at one company is a better shot than one, but
        the same message landing on all of them in one morning reads as a
        blast, and they will compare notes. A day apart, each arrives as its
        own note, and a reply to the first stops the rest before they go.
        """
        seen = set(self.db.companies_sent_on(today))
        ready, held = [], []
        for draft in drafts:
            company = draft["company_id"] if "company_id" in draft.keys() else None
            if company is None:
                ready.append(draft)
                continue
            closed = self.db.company_closed(company)
            if closed:
                held.append((draft, closed))
                if not self.settings.dry_run:
                    self.db.mark_draft(draft["id"], "skipped")
                continue
            if company in seen:
                held.append((draft, "colleague mailed today"))
                continue
            seen.add(company)
            ready.append(draft)
        return ready, held

    def send(self, limit: int, ignore_window: bool = False) -> int:
        settings = self.settings
        settings.require_for_send()
        tag = self._tag("send")
        today = local_date(settings.send_timezone)

        # In per-recipient mode each draft is gated on the window where the
        # recipient sits, so there is no single clock to refuse against. The
        # per-draft check below is what stops a 3am delivery.
        if not settings.per_recipient_timezone:
            ok, why = in_send_window(
                settings.send_timezone, settings.send_window_start,
                settings.send_window_end,
            )
            if not ok and not ignore_window:
                print(f"{tag}: refused, {why}")
                return 0

        already = self.db.sent_count_on(today)
        cap, why_cap = self._todays_cap(today)
        remaining = max(0, cap - already)
        if remaining == 0:
            print(f"{tag}: daily cap reached ({already}/{cap}, {why_cap})")
            return 0

        # The whole queue, filtered by window below. A slice of the top ranks
        # could be all one timezone and leave another zone's tick empty.
        pool = self.db.queued_drafts(limit=None)
        if not pool:
            print(f"{tag}: nothing queued")
            return 0

        # Last line of defence, and it screens the whole pool before the
        # budget is taken rather than after. A draft that still shows
        # [BRACKETS], or that lost its resume, must not go out however it came
        # to be queued -- but it must not consume a send slot either. Screening
        # the truncated list meant two defective drafts at the head of the
        # queue sent nothing on every tick, all day, for as long as they sat
        # there, and nothing marks them, so they sat there forever.
        pool, blocked = self._split_incomplete(pool)
        for email, why in blocked:
            print(f"  BLOCKED {email}: {why}")
        if blocked:
            print(f"{tag}: {len(blocked)} draft(s) blocked as incomplete, "
                  f"passing over them for the next ready draft")
        if not pool:
            print(f"{tag}: no complete drafts to send")
            return 0

        # Held, not skipped: they stay queued and go once the rate recovers.
        catchall_blocked, catchall_why = self._catchall_rule(today)
        if catchall_blocked:
            held_back = [d for d in pool if catchall_blocked(d)]
            if held_back:
                pool = [d for d in pool if not catchall_blocked(d)]
                print(f"{tag}: {len(held_back)} catch-all draft(s) held ({catchall_why})")
            if not pool:
                print(f"{tag}: only held catch-all drafts left")
                return 0

        # The same, for the volume lane's guesses and its own brake.
        guess_blocked, guess_why = self._guess_rule(today)
        if guess_blocked:
            held_back = [d for d in pool if guess_blocked(d)]
            if held_back:
                pool = [d for d in pool if not guess_blocked(d)]
                print(f"{tag}: {len(held_back)} guess draft(s) held ({guess_why})")
            if not pool:
                print(f"{tag}: only held guess drafts left")
                return 0

        # Another lane takes an open draft only once main has used its whole
        # cap for the day: before that, main would have sent it itself, and
        # the main account is the one that has earned its reputation.
        if self.lane != MAIN_LANE:
            full, tally = self.db.lane_at_cap(MAIN_LANE, today)
            if not full:
                open_ = [d for d in pool if not _reserved_row(d)]
                if open_:
                    pool = [d for d in pool if _reserved_row(d)]
                    print(f"{tag}: {len(open_)} open draft(s) left to the main "
                          f"lane ({tally})")
                if not pool:
                    print(f"{tag}: nothing reserved for this lane is ready")
                    return 0

        pool, held = self._space_by_company(pool, today)
        if held:
            print(f"{tag}: {len(held)} held back so colleagues are not mailed "
                  "the same day, or because their company has answered")
        if not pool:
            print(f"{tag}: every ready draft is waiting on a colleague's send")
            return 0

        states = self._window_states(pool, ignore_window)
        if settings.per_recipient_timezone and not ignore_window:
            waiting = Counter(state.where for _row, state in states if not state.open)
            if waiting:
                summary = ", ".join(f"{n} in {z}" for z, n in sorted(waiting.items()))
                print(f"{tag}: {sum(waiting.values())} waiting for their local "
                      f"morning ({summary})")
            if not any(state.open for _row, state in states):
                print(f"{tag}: nobody is inside their local send window right now")
                return 0

        # The rest of the day, less what is owed to follow-ups, spread over
        # the ticks left in each open window. See lanes.spread.
        owed = self._followup_owed(today, cap, ignore_window)
        allowance = max(0, remaining - owed)
        if allowance == 0:
            print(f"{tag}: the rest of today's cap is kept for {owed} follow-up(s)")
            return 0
        drafts = spread(states, min(limit, allowance))
        if not drafts:
            print(f"{tag}: nothing due this tick, {allowance} left for today")
            return 0

        if settings.dry_run:
            print(f"{tag}: DRY RUN, {len(drafts)} message(s) would go out")
            for draft in drafts:
                print(f"{BAR}\nTo: {draft['email']}\nSubject: {draft['subject']}\n")
                print(draft["body"])
            return 0

        sent = 0
        backend = make_backend(settings)
        try:
            sent = self._deliver(backend, drafts, today)
        except smtplib.SMTPAuthenticationError:
            # The likeliest failure on a scheduled run, and worth naming
            # precisely rather than dumping a traceback into the log.
            env = ".env" if self.lane == MAIN_LANE else f".env.{self.lane}"
            print(f"{tag}: SMTP login rejected. Gmail needs an app password, not\n"
                  "      your account password. Enable 2FA, create one at\n"
                  "      https://myaccount.google.com/apppasswords and put it in\n"
                  f"      SMTP_PASS (and IMAP_PASS) in {env}.")
            return 0
        except (smtplib.SMTPException, OSError) as exc:
            print(f"{tag}: could not reach {settings.smtp_host}: "
                  f"{type(exc).__name__}: {exc}")
            return 0
        print(f"{tag}: {sent} delivered, {already + sent}/{cap} today ({why_cap})")
        return sent

    def _deliver(self, backend, drafts: list, today: str) -> int:
        settings = self.settings
        sent = 0
        delivered_mirrors: list[tuple[int, str]] = []
        with backend:
            for index, draft in enumerate(drafts):
                allowed, why = self.db.can_send_to(draft["email"])
                if not allowed:
                    print(f"  skip {draft['email']}: {why}")
                    self.db.mark_draft(draft["id"], "skipped")
                    continue
                try:
                    message, note = message_for_draft(settings, draft)
                except FileNotFoundError as exc:
                    # The PDF moved between queueing and sending, and there is
                    # no hosted link to fall back to.
                    print(f"  BLOCKED {draft['email']}: {exc}")
                    self.db.mark_draft(draft["id"], "failed")
                    continue
                if note:
                    print(f"  {draft['email']}: {note}, using the resume link")
                attempt = self.db.begin_send(
                    draft["contact_id"], draft["email"], draft["subject"], today)
                result = backend.send(message)
                self.db.finish_send(attempt, draft["contact_id"], result.message_id,
                                    result.backend, result.ok, result.error)
                self.db.mark_draft(draft["id"], "sent" if result.ok else "failed")
                if result.ok:
                    sent += 1
                    mirrored = (draft["gmail_message_id"]
                                if "gmail_message_id" in draft.keys() else "")
                    if mirrored:
                        delivered_mirrors.append((draft["id"], mirrored))
                    print(f"  sent {draft['email']} ({draft['company_name']})")
                else:
                    print(f"  FAILED {draft['email']}: {result.error}")
                if index < len(drafts) - 1:
                    time.sleep(pace_delay(settings.send_delay_seconds))
        self._drop_mirrors(delivered_mirrors)
        return sent

    def _drop_mirrors(self, mirrored: list[tuple[int, str]]) -> None:
        """Delete the Gmail copies of drafts SMTP has just accepted.

        Straight after the send loop rather than on the next sync: the window
        in which a copy sits in Drafts for a message that has already gone out
        is exactly the window in which it can be sent a second time by hand.

        A failure here is not fatal and must not fail the run, which has
        already delivered real mail. The copy stays recorded as mirrored and
        the next mirror sync drops it.

        Main only: the copies live in the main account's Drafts, which
        another lane cannot reach with its own login. When another lane sends
        a mirrored draft, the main mirror sync that runs right after it in
        the same tick (scripts/job.sh) clears the copy.
        """
        if (not mirrored or not self.settings.mirror_to_drafts
                or self.lane != MAIN_LANE):
            return
        try:
            with GmailDrafts(self.settings) as gmail:
                for draft_id, message_id in mirrored:
                    if gmail.drop(message_id):
                        self.db.clear_draft_mirror(draft_id)
        except (imaplib.IMAP4.error, OSError) as exc:
            print(f"  note: {len(mirrored)} Gmail draft(s) left to the next "
                  f"sync ({type(exc).__name__}: {exc})")

    def digest(self, days: int = 7, send_it: bool = True) -> str:
        return build_digest(self.db, self.settings, days, send_it)

    def summary(self) -> None:
        print_summary(self.db, self.settings, self.fetcher, self.personalizer)
