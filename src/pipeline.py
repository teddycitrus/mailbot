"""The outreach pipeline: one class per stage of the funnel.

Split from main.py so the command line layer stays thin and this file stays
readable. Stages are deliberately independent so each can be run and inspected
on its own before the next.
"""

from __future__ import annotations

import smtplib
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Settings
from .database import Database
from .emailer import (
    greeting_name, has_resume_link, resume_link_line, with_resume_link,
    build_message, in_send_window, load_template, local_date, local_now,
    local_window_open, make_backend, pace_delay, unedited_markers,
    validate_template,
)
from .finder import discover as discover_companies, enrich_company
from .gh_discovery import discover as discover_orgs
from .github_source import GitHubClient
from .hn_source import fetch_posts, fetch_recent_posts
from .llm import Personalizer
from .models import (
    Company, Contact, ENRICHED, NO_CONTACTS, PENDING, SRC_SCRAPED,
)
from .scraper import Fetcher
from .reporting import BAR, _print_table, build_digest, print_summary
from .verifier import (
    Verifier, first_name_from_email, is_never_send, is_role_account,
)

class Pipeline:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.db_path)
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

    # ---------------- stages ----------------

    def discover(self, limit: int) -> int:
        found, stats = discover_companies(self.fetcher, self.settings, self.db, limit=limit)
        for company in found:
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
            for contact in result.contacts:
                allowed, why = self.db.can_send_to(contact.email)
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
                             contact.role[:34] or contact.source))
            self.db.set_company_status(
                company["id"], ENRICHED if kept else NO_CONTACTS, result.note
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
        for post in posts[:limit]:
            if not post.company or not post.domain:
                continue
            company_id = self.db.upsert_company(Company(
                name=post.company, domain=post.domain, website=post.website,
                location=post.location, one_liner=post.text[:180].replace("\n", " "),
                description=post.text[:1200], is_hiring=True, source="hn",
                status=ENRICHED,
            ))
            if self.db.company_has_contacted(company_id):
                rows.append((post.company, "-", "skip", 0, "already contacted"))
                continue
            best = None
            for address in post.emails:
                if is_never_send(address) or address.split("@")[1] != post.domain:
                    continue
                verdict = self.verifier.verify(address, source=SRC_SCRAPED)
                if verdict.confidence <= 0:
                    continue
                # A named address beats a shared inbox for a personal note.
                if best is None or (verdict.confidence > best[1].confidence):
                    best = (address, verdict)
                if not is_role_account(address):
                    break
            if best is None:
                rows.append((post.company, "-", "-", 0, "no usable address"))
                continue
            address, verdict = best
            allowed, why = self.db.can_send_to(address)
            if not allowed:
                rows.append((post.company, address, "skip", 0, why))
                continue
            contact_id = self.db.upsert_contact(Contact(
                email=address, company_id=company_id, source=SRC_SCRAPED,
                role="posted the hiring ad", verify_status=verdict.status,
                verify_detail=f"published on HN: {verdict.detail}",
                confidence=verdict.confidence, status=PENDING,
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

    def queue(self, limit: int) -> int:
        template = load_template(self.settings.template_path)
        validate_template(template)
        holes = unedited_markers(f"{template.subject}\n{template.body}")
        if holes:
            print(f"warning: template still has {len(holes)} unedited placeholder(s): "
                  f"{', '.join(holes[:3])}")
            print("         drafts will queue for preview but send() will refuse them")
        # The resume is an attachment first. The hosted link only appears
        # when there is no PDF to attach, because a message carrying both
        # shows the recipient two resume chips and reads as sloppy.
        attachments = []
        resume_link = ""
        if self.settings.resume_path.exists():
            attachments.append(str(self.settings.resume_path))
        elif self.settings.resume_link:
            resume_link = resume_link_line(self.settings.resume_link)
            print(f"warning: resume not found at {self.settings.resume_path}, "
                  "falling back to the resume link in the body")
        else:
            print(f"warning: resume not found at {self.settings.resume_path} "
                  "and no RESUME_LINK is set; queuing without either, "
                  "send() will refuse these drafts")

        contacts = self.db.contacts_by_status(PENDING, limit=limit * 3)
        queued = 0
        rows = []
        for row in contacts:
            if queued >= limit:
                break
            if row["confidence"] < self.settings.min_confidence:
                rows.append((row["email"], "skip",
                             f"confidence {row['confidence']} < "
                             f"{self.settings.min_confidence}"))
                continue
            allowed, why = self.db.can_send_to(row["email"])
            if not allowed:
                rows.append((row["email"], "skip", why))
                continue
            if row["company_id"] and self.db.company_has_contacted(row["company_id"]):
                rows.append((row["email"], "skip", "company already contacted"))
                continue
            # The template opens "Dear {first_name}," so a missing name used to
            # render "Dear there,". Recover a given name from the address where
            # the local part is plainly one, and otherwise address the company:
            # "Dear Datrics," is a fair greeting for a shared inbox, where
            # guessing a person's name would not be. Resolved before
            # personalising so a skip costs no LLM call.
            first = (row["first_name"]
                     or first_name_from_email(row["email"])
                     or greeting_name(row["company_name"] or ""))
            if not first:
                rows.append((row["email"], "skip", "no name and no company"))
                continue
            note = self.personalizer.line_for(
                row["company_name"] or "", row["one_liner"] or "",
                row["description"] or "", row["role"] or "",
            )
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
            rows.append((row["email"], "queued", subject[:40]))
        print(f"queue: {queued} drafts ready ({self.personalizer.budget})")
        _print_table(rows, ("email", "status", "detail"))
        return queued

    def _split_incomplete(self, drafts: list) -> tuple[list, list[tuple[str, str]]]:
        """Separate drafts that are safe to send from ones that are not finished."""
        ready, blocked = [], []
        want_resume = bool(str(self.settings.resume_path))
        for draft in drafts:
            holes = unedited_markers(f"{draft['subject']}\n{draft['body']}")
            if holes:
                blocked.append((draft["email"],
                                f"unedited template: {', '.join(holes[:2])}"))
                continue
            attachments = [a for a in (draft["attachments"] or "").split("|") if a]
            # A draft carrying the hosted link instead of the PDF is complete:
            # that is the fallback the queue applies when there is nothing to
            # attach. One or the other must be there, never neither.
            if want_resume and not attachments and not has_resume_link(draft["body"]):
                blocked.append((draft["email"], "no resume attached"))
                continue
            missing = [a for a in attachments if not Path(a).exists()]
            if missing and not self.settings.resume_link:
                blocked.append((draft["email"], "attachment file is missing"))
                continue
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
            return settings.daily_send_limit, "ramp off"
        return settings.daily_cap(
            self.db.active_send_days(today),
            self.db.recent_bounce_pct(),
        )

    def followup(self, limit: int, ignore_window: bool = False) -> int:
        """Send one polite nudge to people who never answered.

        Deliberately capped by FOLLOWUP_MAX_TOTAL_SENDS (2 by default: the
        original plus one bump). Anyone who replied, bounced or asked to stop
        is excluded by the query, so this cannot turn into a drip campaign.
        """
        settings = self.settings
        if not settings.followup_enabled:
            print("followup: disabled")
            return 0
        settings.require_for_send()
        template = load_template(settings.followup_template_path)
        validate_template(template)

        today = local_date(settings.send_timezone)
        already = self.db.sent_count_on(today)
        cap, why_cap = self._todays_cap(today)
        remaining = max(0, cap - already)
        if remaining == 0:
            print(f"followup: daily cap reached ({already}/{cap}, {why_cap})")
            return 0

        due = self.db.due_for_followup(
            settings.followup_after_days, settings.followup_max_total_sends,
            limit=max(limit * 5, 50),
        )
        if not due:
            print("followup: nobody due")
            return 0

        # Same rule as the first email: attach the PDF, and only reach for
        # the hosted link when there is no PDF to attach.
        attachments = ([str(settings.resume_path)]
                       if settings.resume_path.exists() else [])
        if not attachments and not settings.resume_link:
            print("followup: resume missing and no RESUME_LINK set, refusing to send")
            return 0
        if not attachments:
            print(f"followup: resume missing at {settings.resume_path}, "
                  "falling back to the resume link")

        sent = 0
        backend = make_backend(settings)
        try:
            with backend:
                for row in due:
                    if sent >= min(limit, remaining):
                        break
                    allowed, why = self.db.can_send_to_again(row["email"])
                    if not allowed:
                        print(f"  skip {row['email']}: {why}")
                        continue
                    if settings.per_recipient_timezone and not ignore_window:
                        open_now, where = local_window_open(
                            row["company_location"] or "", settings.send_timezone,
                            settings.send_window_start, settings.send_window_end,
                        )
                        if not open_now:
                            continue
                    first = (row["first_name"]
                             or first_name_from_email(row["email"])
                             or greeting_name(row["company_name"] or ""))
                    if not first:
                        print(f"  skip {row['email']}: no name and no company")
                        continue
                    subject, body = template.render({
                        "first_name": first,
                        "original_subject": (row["first_subject"] or "").lstrip("Re: "),
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
                    result = backend.send(message)
                    self.db.record_send(
                        row["id"], row["email"], subject, result.message_id,
                        result.backend, today,
                        "sent" if result.ok else "failed", result.error,
                    )
                    if result.ok:
                        sent += 1
                        print(f"  followed up {row['email']} ({row['company_name']})")
                    else:
                        print(f"  FAILED {row['email']}: {result.error}")
                    time.sleep(pace_delay(settings.send_delay_seconds))
        except smtplib.SMTPAuthenticationError:
            print("followup: SMTP login rejected, see send for details")
            return 0
        except (smtplib.SMTPException, OSError) as exc:
            print(f"followup: could not reach {settings.smtp_host}: {exc}")
            return 0
        print(f"followup: {sent} sent, {len(due)} were due")
        return sent

    def _split_by_local_window(self, drafts: list):
        """Separate drafts whose recipient is currently in their own morning."""
        settings = self.settings
        now_here, waiting = [], []
        for draft in drafts:
            location = draft["company_location"] if "company_location" in draft.keys() else ""
            open_now, where = local_window_open(
                location or "", settings.send_timezone,
                settings.send_window_start, settings.send_window_end,
            )
            (now_here if open_now else waiting).append(
                draft if open_now else (draft, where)
            )
        return now_here, waiting

    def send(self, limit: int, ignore_window: bool = False) -> int:
        settings = self.settings
        settings.require_for_send()
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
                print(f"send: refused, {why}")
                return 0

        already = self.db.sent_count_on(today)
        cap, why_cap = self._todays_cap(today)
        remaining = max(0, cap - already)
        if remaining == 0:
            print(f"send: daily cap reached ({already}/{cap}, {why_cap})")
            return 0

        # Pull more than the budget so recipients outside their local morning
        # can be skipped without starving the run.
        pool = self.db.queued_drafts(limit=max(limit * 5, 50))
        if not pool:
            print("send: nothing queued")
            return 0

        if settings.per_recipient_timezone and not ignore_window:
            pool, waiting = self._split_by_local_window(pool)
            if waiting:
                zones: dict[str, int] = {}
                for _, where in waiting:
                    zones[where] = zones.get(where, 0) + 1
                summary = ", ".join(f"{n} in {z}" for z, n in sorted(zones.items()))
                print(f"send: {len(waiting)} waiting for their local morning ({summary})")
            if not pool:
                print("send: nobody is inside their local send window right now")
                return 0

        budget = min(limit, remaining)
        drafts = pool[:budget]

        # Last line of defence. A draft that still shows [BRACKETS], or that
        # lost its resume attachment, is incomplete and must not go out however
        # it came to be queued.
        drafts, blocked = self._split_incomplete(drafts)
        for email, why in blocked:
            print(f"  BLOCKED {email}: {why}")
        if blocked:
            print(f"send: {len(blocked)} draft(s) blocked as incomplete")
        if not drafts:
            print("send: no complete drafts to send")
            return 0

        if settings.dry_run:
            print(f"send: DRY RUN, {len(drafts)} message(s) would go out")
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
            print("send: SMTP login rejected. Gmail needs an app password, not\n"
                  "      your account password. Enable 2FA, create one at\n"
                  "      https://myaccount.google.com/apppasswords and put it in\n"
                  "      SMTP_PASS (and IMAP_PASS) in .env.")
            return 0
        except (smtplib.SMTPException, OSError) as exc:
            print(f"send: could not reach {settings.smtp_host}: "
                  f"{type(exc).__name__}: {exc}")
            return 0
        print(f"send: {sent} delivered, {already + sent}/{cap} today ({why_cap})")
        return sent

    def _deliver(self, backend, drafts: list, today: str) -> int:
        settings = self.settings
        sent = 0
        with backend:
            for index, draft in enumerate(drafts):
                allowed, why = self.db.can_send_to(draft["email"])
                if not allowed:
                    print(f"  skip {draft['email']}: {why}")
                    self.db.mark_draft(draft["id"], "skipped")
                    continue
                attachments = [a for a in (draft["attachments"] or "").split("|") if a]
                body = draft["body"]
                try:
                    message = build_message(
                        draft["subject"], body, draft["email"],
                        " ".join(p for p in (draft["first_name"], draft["last_name"]) if p),
                        settings.from_email, settings.from_name,
                        settings.reply_to, settings.unsubscribe_mailto, attachments,
                    )
                except FileNotFoundError as exc:
                    # The PDF moved between queueing and sending. Send the link
                    # instead of dropping the resume from the message entirely.
                    if not settings.resume_link:
                        print(f"  BLOCKED {draft['email']}: {exc}")
                        self.db.mark_draft(draft["id"], "failed")
                        continue
                    print(f"  {draft['email']}: {exc}, using the resume link")
                    body = with_resume_link(body, settings.resume_link)
                    message = build_message(
                        draft["subject"], body, draft["email"],
                        " ".join(p for p in (draft["first_name"], draft["last_name"]) if p),
                        settings.from_email, settings.from_name,
                        settings.reply_to, settings.unsubscribe_mailto,
                    )
                result = backend.send(message)
                self.db.record_send(
                    draft["contact_id"], draft["email"], draft["subject"],
                    result.message_id, result.backend, today,
                    "sent" if result.ok else "failed", result.error,
                )
                self.db.mark_draft(draft["id"], "sent" if result.ok else "failed")
                if result.ok:
                    sent += 1
                    print(f"  sent {draft['email']} ({draft['company_name']})")
                else:
                    print(f"  FAILED {draft['email']}: {result.error}")
                if index < len(drafts) - 1:
                    time.sleep(pace_delay(settings.send_delay_seconds))
        return sent

    def digest(self, days: int = 7, send_it: bool = True) -> str:
        return build_digest(self.db, self.settings, days, send_it)

    def summary(self) -> None:
        print_summary(self.db, self.settings, self.fetcher, self.personalizer)
