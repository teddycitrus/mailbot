"""Command line entry point.

Thin wrapper over Pipeline. Scheduling lives in scripts/, and the stages
themselves live in pipeline.py.
"""

from __future__ import annotations

import argparse
import sys

from .config import ConfigError, Settings
from .database import Database
from .doctor import FAIL, run_checks, scan_build
from .inbox import scan_inbox
from .pipeline import BAR, Pipeline, _print_table


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
    p = sub.add_parser("queue", help="render personalised drafts")
    p.add_argument("--limit", type=int, default=25)
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

    if args.command == "init":
        Database(settings.db_path).close()
        print(f"initialised {settings.db_path}")
        return 0

    pipeline = Pipeline(settings)
    try:
        if args.command == "discover":
            pipeline.discover(args.limit)
        elif args.command == "hn":
            pipeline.import_hn(args.limit, args.months)
        elif args.command == "gh":
            pipeline.discover_github(args.limit)
        elif args.command == "enrich":
            pipeline.enrich(args.limit)
        elif args.command == "queue":
            pipeline.queue(args.limit)
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
