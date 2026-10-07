"""Command line entry point.

Thin wrapper over Pipeline. Scheduling lives in scripts/, and the stages
themselves live in pipeline.py.
"""

from __future__ import annotations

import argparse
import contextlib
import sys

from .alerts import bounce_check, health_check, print_bounce_report, print_report
from .config import ConfigError, Settings
from .database import Database
from .doctor import FAIL, run_checks, scan_build
from .inbox import scan_inbox
from .locking import Busy, default_lock_path, single_run
from .mirror import sync as mirror_sync
from .pipeline import BAR, Pipeline, _print_table
from .reverify import catch_up, recover
from .verifier import port25_reachable

# Commands that only read, or that run something long-lived of their own. The
# rest take the install-wide lock, because the scheduled jobs overlap and two
# of them writing at once is how a draft ends up mirrored to Gmail twice.
# See locking.py.
# bounce-report is here deliberately. It writes only a single log row, and an
# alert about a bad day must never be the thing that gets skipped because a
# drafting run happened to hold the lock.
LOCK_EXEMPT = frozenset({
    "dashboard", "verify-build", "doctor", "stats", "preview", "health",
    "bounce-report",
})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mailbot", description="Cold outreach to early-stage ML startups."
    )
    parser.add_argument("--env", default=".env", help="path to env file")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the database and exit")
    p = sub.add_parser("discover", help="find qualifying companies")
    p.add_argument("--limit", type=int, default=15)
    p = sub.add_parser("hn", help="import contacts from the HN who-is-hiring threads")
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--months", type=int, default=1,
                   help="how many monthly threads back to read")
    p = sub.add_parser("gh", help="find companies via GitHub org search (non-YC)")
    p.add_argument("--limit", type=int, default=40)
    p = sub.add_parser("enrich", help="find a contact at each new company")
    p.add_argument("--limit", type=int, default=15)
    p = sub.add_parser("priority", help="seed the companies named in priority.txt")
    p.add_argument("--resolve", action="store_true",
                   help="look up the domain for entries that have none, and "
                        "write it back to the file")
    p.add_argument("--limit", type=int, default=0,
                   help="most domains to resolve in one pass (0 = all)")
    p = sub.add_parser("queue", help="render personalised drafts")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--target", type=int, default=0,
                   help="stop once this many drafts are waiting "
                        "(0 uses QUEUE_TARGET, which is what the drafting job "
                        "runs on; pass --target -1 to render regardless)")
    p = sub.add_parser("mirror", help="copy queued drafts into Gmail Drafts")
    p.add_argument("--limit", type=int, default=50,
                   help="most copies to push in one pass")
    p.add_argument("--days", type=int, default=7,
                   help="how far back to read Sent for drafts you sent by hand")
    p = sub.add_parser("send", help="send queued drafts")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--ignore-window", action="store_true",
                   help="send outside the configured local hours")
    p = sub.add_parser("digest", help="email a summary of the last N days")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--print", dest="print_only", action="store_true",
                   help="print instead of emailing")
    p = sub.add_parser("followup", help="send one nudge to people who never replied")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--ignore-window", action="store_true")
    p = sub.add_parser("run", help="discover, enrich, queue and send in one pass")
    p.add_argument("--limit", type=int, default=15)
    p.add_argument("--ignore-window", action="store_true")
    # Scan depth is tunable because the two callers want opposite things: the
    # once-a-day prep run wants a full sweep, while a 15-minute tick only needs
    # the mail that arrived since the previous tick. Each examined message is a
    # full RFC822 fetch, so the default sweep costs about ninety seconds.
    for _name, _help in (("bounces", "scan the mailbox for bounces, replies and opt-outs"),
                         ("inbox", "alias for bounces")):
        p = sub.add_parser(_name, help=_help)
        p.add_argument("--days", type=int, default=30,
                       help="how far back to look")
        p.add_argument("--limit", type=int, default=500,
                       help="most recent messages to examine")
    p = sub.add_parser("suppress", help="never contact an address")
    p.add_argument("email")
    sub.add_parser("stats", help="print the summary tables")
    p = sub.add_parser("health", help="check everything and alert if it is broken")
    p.add_argument("--no-email", dest="email", action="store_false",
                   help="print the report without mailing it")
    p = sub.add_parser(
        "bounce-report",
        help="email a warning if today's bounce rate went over the threshold")
    p.add_argument("--day", default="",
                   help="the local date to judge (default: today)")
    p.add_argument("--no-email", dest="email", action="store_false",
                   help="print the report without mailing it")
    p = sub.add_parser(
        "reverify",
        help="re-probe contacts whose verdict recorded a failed probe")
    p.add_argument("--limit", type=int, default=0,
                   help="most contacts to re-probe in one pass (0 = all)")
    p.add_argument("--no-reopen", dest="reopen", action="store_false",
                   help="leave parked companies alone instead of re-enriching")
    p = sub.add_parser(
        "reprobe",
        help="if port 25 works right now, verify whatever waited for it")
    p.add_argument("--limit", type=int, default=25,
                   help="most contacts to re-probe in one pass")
    p.add_argument("--enrich", type=int, default=10,
                   help="then enrich up to this many companies (0 = none)")
    sub.add_parser("doctor", help="check everything the scheduled jobs rely on")
    p = sub.add_parser("verify-build", help="check a built exe carries no private data")
    p.add_argument("exe", nargs="?", default="dist/mailbot.exe")
    p = sub.add_parser("dashboard", help="open the local setup and progress console")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--no-browser", action="store_true")
    p = sub.add_parser("preview", help="show queued drafts without sending")
    p.add_argument("--limit", type=int, default=3)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.load(args.env)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.command == "verify-build":
        checks = scan_build(args.exe, settings)
        _print_table([(c.name, c.state, c.detail) for c in checks],
                     ("check", "state", "detail"))
        leaks = [c for c in checks if c.state == FAIL]
        print()
        print(f"DO NOT SHARE: {len(leaks)} leak(s)" if leaks
              else "safe to publish: no private data found in the build")
        return 1 if leaks else 0

    if args.command == "dashboard":
        from .webapp import serve
        serve(args.host, args.port, open_browser=not args.no_browser)
        return 0

    if args.command == "health":
        report = health_check(settings, notify=args.email)
        print_report(report)
        return 1 if report.failures else 0

    if args.command == "bounce-report":
        db = Database(settings.db_path)
        try:
            report = bounce_check(settings, db, args.day, notify=args.email)
        finally:
            db.close()
        print_bounce_report(report)
        # Non-zero so the scheduled task shows it and the wrapper can raise a
        # desktop notification without having to parse the output.
        return 1 if report.over else 0

    if args.command == "init":
        Database(settings.db_path).close()
        print(f"initialised {settings.db_path}")
        return 0

    # Held for the whole run, released by the finally below. A job that cannot
    # get it stands down rather than queueing: every one of these runs again
    # within a couple of hours, and a skipped tick costs nothing next to two
    # runs writing over each other.
    stack = contextlib.ExitStack()
    if args.command not in LOCK_EXEMPT:
        try:
            stack.enter_context(single_run(default_lock_path(settings.db_path)))
        except Busy as exc:
            print(f"{args.command}: {exc}, skipping this run")
            return 0

    pipeline = Pipeline(settings)
    try:
        if args.command == "discover":
            pipeline.discover(args.limit)
        elif args.command == "hn":
            pipeline.import_hn(args.limit, args.months)
        elif args.command == "gh":
            pipeline.discover_github(args.limit)
        elif args.command == "priority":
            pipeline.priority(args.resolve, args.limit)
        elif args.command == "enrich":
            pipeline.enrich(args.limit)
        elif args.command == "queue":
            # 0 means "use the configured depth", which is what the drafting
            # job wants. A negative target turns the check off, for rendering
            # a batch by hand however deep the queue already is.
            target = settings.queue_target if args.target == 0 else args.target
            pipeline.queue(args.limit, max(0, target))
        elif args.command == "mirror":
            if not settings.mirror_to_drafts:
                print("mirror: MIRROR_TO_DRAFTS is off, nothing to do")
                return 0
            report = mirror_sync(settings, pipeline.db, args.limit, args.days)
            print(f"mirror: {len(report.pushed)} copied to Drafts, "
                  f"{len(report.dropped)} cleared after sending, "
                  f"{len(report.reconciled)} you had already sent by hand, "
                  f"{len(report.skipped)} skipped")
            _print_table(report.rows(), ("email", "state", "detail"))
        elif args.command == "send":
            pipeline.send(args.limit, args.ignore_window)
            pipeline.summary()
        elif args.command == "followup":
            pipeline.followup(args.limit, args.ignore_window)
        elif args.command == "digest":
            body = pipeline.digest(args.days, send_it=not args.print_only)
            if args.print_only:
                print(body)
        elif args.command == "run":
            pipeline.discover(args.limit)
            pipeline.enrich(args.limit)
            pipeline.queue(args.limit)
            pipeline.send(args.limit, args.ignore_window)
            pipeline.summary()
        elif args.command in ("bounces", "inbox"):
            settings.require_for_bounces()
            scan = scan_inbox(settings, pipeline.db, args.days, args.limit)
            print(f"inbox: examined {scan.examined} message(s) -> "
                  f"{len(scan.bounced)} bounced, {len(scan.replied)} replied, "
                  f"{len(scan.opted_out)} opted out, "
                  f"{len(scan.drafted)} reply draft(s) saved")
            rows = ([(a, "bounce", "hard" if h else "soft") for a, h in scan.bounced]
                    + [(a, "reply", "") for a in scan.replied]
                    + [(a, "opt-out", "suppressed") for a in scan.opted_out]
                    + [(a, "draft", "in Drafts, not sent") for a in scan.drafted])
            _print_table(rows, ("email", "kind", "detail"))
        elif args.command == "suppress":
            pipeline.db.suppress(args.email, "manual")
            print(f"suppressed {args.email}")
        elif args.command == "stats":
            pipeline.summary()
        elif args.command == "reverify":
            report = recover(pipeline.db, pipeline.verifier, args.limit,
                             reopen=args.reopen)
            print(f"reverify: {report.examined} re-probed, "
                  f"{len(report.improved)} improved, "
                  f"{len(report.unchanged)} unchanged, "
                  f"{len(report.dropped)} suppressed as undeliverable "
                  f"({report.drafts_pulled} queued draft(s) pulled); "
                  f"{report.cache_cleared} stale domain(s) cleared, "
                  f"{report.companies_reopened} company(s) back in the enrich "
                  f"queue, {report.mx_reopened} of {report.mx_rechecked} parked "
                  "no-MX domain(s) resolve again and were reopened")
            _print_table(report.rows(), ("email", "confidence", "state", "detail"))
        elif args.command == "reprobe":
            # Runs every half hour whether or not the laptop is on, so the
            # common outcome is this early exit, and it must cost nothing.
            ok, detail = port25_reachable(pipeline.verifier.relay)
            if not ok:
                print(f"reprobe: {detail}, nothing to do until it is back")
                return 0
            report = catch_up(pipeline.db, pipeline.verifier, args.limit)
            print(f"reprobe: {detail}; {report.examined} re-probed, "
                  f"{len(report.improved)} improved, "
                  f"{len(report.dropped)} suppressed as undeliverable, "
                  f"{report.companies_reopened} company(s) back in the "
                  "enrich queue")
            _print_table(report.rows(), ("email", "confidence", "state", "detail"))
            if args.enrich and not pipeline.verifier.throttled:
                pipeline.enrich(args.enrich)
        elif args.command == "doctor":
            checks = run_checks(settings)
            _print_table([(c.name, c.state, c.detail) for c in checks],
                         ("check", "state", "detail"))
            broken = [c for c in checks if c.state == FAIL]
            print()
            print(f"{len(broken)} problem(s)" if broken else "all good")
            return 1 if broken else 0
        elif args.command == "preview":
            drafts = pipeline.db.queued_drafts(limit=args.limit)
            if not drafts:
                print("nothing queued")
            for draft in drafts:
                print(f"{BAR}\nTo: {draft['email']}  ({draft['company_name']})")
                print(f"Subject: {draft['subject']}\n")
                print(draft["body"])
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"missing file: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"template error: {exc}", file=sys.stderr)
        return 2
    finally:
        pipeline.close()
        stack.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
