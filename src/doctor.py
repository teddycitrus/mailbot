"""Health check: is this thing still going to work tomorrow morning?

Everything the scheduled jobs depend on gets verified here, because the failure
mode that matters is silent. If the Gmail app password is revoked, sends fail,
and the weekly digest that would have told you also fails, so nothing reaches
you. Run `python -m src.main doctor` when in doubt.
"""

from __future__ import annotations

import imaplib
import smtplib
import ssl
from dataclasses import dataclass
from pathlib import Path

import requests

from .config import ConfigError, Settings
from .database import Database
from .emailer import load_template, unedited_markers, validate_template

OK, WARN, FAIL = "ok", "warn", "FAIL"


@dataclass
class Check:
    name: str
    state: str
    detail: str = ""


def _smtp(settings) -> Check:
    if not (settings.smtp_host and settings.smtp_user and settings.smtp_pass):
        return Check("smtp login", FAIL, "SMTP_HOST/USER/PASS not all set")
    try:
        conn = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
        conn.ehlo()
        conn.starttls(context=ssl.create_default_context())
        conn.ehlo()
        conn.login(settings.smtp_user, settings.smtp_pass)
        conn.quit()
        return Check("smtp login", OK, settings.smtp_user)
    except smtplib.SMTPAuthenticationError:
        return Check("smtp login", FAIL,
                     "rejected. Gmail app passwords die when you change your "
                     "Google password. Make a new one at "
                     "myaccount.google.com/apppasswords")
    except Exception as exc:
        return Check("smtp login", FAIL, f"{type(exc).__name__}: {exc}")


def _imap(settings) -> Check:
    if not (settings.imap_host and settings.imap_user and settings.imap_pass):
        return Check("imap login", WARN, "not configured, bounces unmonitored")
    try:
        conn = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
        conn.login(settings.imap_user, settings.imap_pass)
        conn.select("INBOX")
        conn.logout()
        return Check("imap login", OK, settings.imap_user)
    except Exception as exc:
        return Check("imap login", FAIL, f"{type(exc).__name__}: {exc}")


def _groq(settings) -> Check:
    if not settings.groq_api_key:
        return Check("groq", WARN, "no key, alignment paragraph always omitted")
    try:
        resp = requests.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {settings.groq_api_key}"}, timeout=15)
        if resp.status_code != 200:
            return Check("groq", FAIL, f"HTTP {resp.status_code}")
        ids = {m["id"] for m in resp.json().get("data", [])}
        if settings.groq_model not in ids:
            return Check("groq", FAIL,
                         f"model {settings.groq_model} not available to this key")
        return Check("groq", OK, settings.groq_model)
    except Exception as exc:
        return Check("groq", FAIL, f"{type(exc).__name__}: {exc}")


def _github(settings) -> Check:
    headers = {"User-Agent": "mailbot/0.1"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    try:
        resp = requests.get("https://api.github.com/rate_limit",
                            headers=headers, timeout=15)
        if resp.status_code == 401:
            return Check("github token", FAIL,
                         "rejected, likely expired. Classic tokens expire; make "
                         "a new no-scope one at github.com/settings/tokens")
        core = resp.json()["resources"]["core"]
        limit = core["limit"]
        if settings.github_token and limit < 1000:
            return Check("github token", WARN,
                         f"only {limit}/hr, token may not be applying")
        state = OK if settings.github_token else WARN
        detail = f"{core['remaining']}/{limit} per hour"
        if not settings.github_token:
            detail += " (no token; enrichment is slower)"
        return Check("github token", state, detail)
    except Exception as exc:
        return Check("github token", WARN, f"{type(exc).__name__}: {exc}")


def _template(settings) -> Check:
    try:
        tpl = load_template(settings.template_path)
        validate_template(tpl)
    except (ValueError, FileNotFoundError, OSError) as exc:
        return Check("template", FAIL, str(exc)[:120])
    holes = unedited_markers(f"{tpl.subject}\n{tpl.body}")
    if holes:
        return Check("template", FAIL, f"unedited placeholders: {holes[:2]}")
    return Check("template", OK, str(settings.template_path))


def _resume(settings) -> Check:
    path = settings.resume_path
    if not path.exists():
        # The hosted link is the fallback, so a missing PDF degrades the email
        # rather than stopping it. Without either, nothing can go out.
        if settings.resume_link:
            return Check("resume", WARN,
                         f"missing at {path}; sends fall back to RESUME_LINK")
        return Check("resume", FAIL, f"missing at {path}; every send is blocked")
    size = path.stat().st_size
    if size < 1000:
        return Check("resume", FAIL, f"only {size} bytes, looks truncated")
    head = path.read_bytes()[:5]
    if head != b"%PDF-":
        return Check("resume", FAIL, "not a PDF")
    return Check("resume", OK, f"{path} ({size // 1024}KB)")


def _queue(settings, db) -> Check:
    stats = db.stats()
    queued = stats["queued"]
    pool = db.conn.execute(
        "SELECT COUNT(*) FROM companies WHERE status = 'new'").fetchone()[0]
    if queued == 0 and pool == 0:
        return Check("queue", FAIL, "nothing queued and no companies left to enrich")
    if queued < settings.daily_send_limit:
        return Check("queue", WARN,
                     f"{queued} queued, below the daily limit of "
                     f"{settings.daily_send_limit}; {pool} companies still to enrich")
    return Check("queue", OK, f"{queued} queued, {pool} companies still to enrich")


def _mirror(settings) -> Check:
    """Can the drafting job still reach the Drafts folder?

    Worth its own check because the mirror is the fallback for this machine
    being unavailable, and a fallback that has quietly stopped working is
    worse than none: you would only find out on the morning you needed it.
    """
    if not getattr(settings, "mirror_to_drafts", False):
        return Check("gmail drafts", WARN,
                     "MIRROR_TO_DRAFTS=false, no fallback if this machine is off")
    if not (settings.imap_host and settings.imap_user and settings.imap_pass):
        return Check("gmail drafts", FAIL, "needs IMAP_HOST/USER/PASS")
    try:
        from .inbox import drafts_mailbox
        conn = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
        conn.login(settings.imap_user, settings.imap_pass)
        mailbox = drafts_mailbox(conn)
        typ, _ = conn.select(mailbox)
        conn.logout()
        if typ != "OK":
            return Check("gmail drafts", FAIL, f"cannot open {mailbox}")
        return Check("gmail drafts", OK, mailbox)
    except Exception as exc:
        return Check("gmail drafts", FAIL, f"{type(exc).__name__}: {exc}")


def _priority(settings) -> Check:
    """The hand-written list, and how much of it is still unusable."""
    from .priority import load
    path = getattr(settings, "priority_path", None)
    if not path or not Path(path).exists():
        return Check("priority list", WARN, "no config/priority.txt")
    entries = load(path)
    if not entries:
        return Check("priority list", WARN, f"{path} is empty")
    unresolved = [e.name for e in entries if not e.domain]
    if unresolved:
        return Check("priority list", WARN,
                     f"{len(entries) - len(unresolved)}/{len(entries)} resolved, "
                     f"needs a domain: {', '.join(unresolved[:3])}"
                     + (" ..." if len(unresolved) > 3 else ""))
    return Check("priority list", OK, f"{len(entries)} companies")


def run_checks(settings: Settings) -> list[Check]:
    checks = [_template(settings), _resume(settings)]
    try:
        settings.require_for_send()
        checks.append(Check("send config", OK, f"backend={settings.email_backend}"))
    except ConfigError as exc:
        checks.append(Check("send config", FAIL, str(exc)))
    checks += [_smtp(settings), _imap(settings), _mirror(settings),
               _priority(settings), _groq(settings), _github(settings)]
    try:
        db = Database(settings.db_path)
        checks.append(_queue(settings, db))
        db.close()
    except Exception as exc:
        checks.append(Check("database", FAIL, f"{type(exc).__name__}: {exc}"))
    if settings.dry_run:
        checks.append(Check("dry run", WARN, "DRY_RUN=true, nothing will be sent"))
    return checks


# ------------------------------------------------- pre-distribution check

# Files that must never end up inside a build. Each is either a credential or
# somebody's personal data, and an executable is trivially unpacked.
NEVER_BUNDLE = ("outreach.db", "assets/*.pdf", ".env")


def scan_build(exe_path, settings: Settings) -> list[Check]:
    """Prove a built executable carries none of this machine's private data.

    Run before publishing. The failure this catches is silent and permanent:
    once a build with a live app password is on the internet, it is on the
    internet.
    """
    from pathlib import Path

    exe = Path(exe_path)
    if not exe.exists():
        return [Check("build", FAIL, f"no such file: {exe}")]

    blob = exe.read_bytes()
    checks = [Check("build", OK, f"{exe.name}, {len(blob) / 1_000_000:.1f} MB")]

    def leaked(needle: str) -> bool:
        if not needle or len(needle) < 6:
            return False
        raw = needle.encode()
        return raw in blob or needle.encode("utf-16-le") in blob

    secrets = {
        "SMTP_PASS": settings.smtp_pass,
        "IMAP_PASS": settings.imap_pass,
        "GROQ_API_KEY": settings.groq_api_key,
        "GITHUB_TOKEN": settings.github_token,
        "RESEND_API_KEY": settings.resend_api_key,
    }
    for name, value in secrets.items():
        if not value:
            continue
        checks.append(Check(f"secret {name}", FAIL if leaked(value) else OK,
                            "FOUND IN BUILD" if leaked(value) else "not present"))

    if settings.from_email:
        checks.append(Check("your email address",
                            FAIL if leaked(settings.from_email) else OK,
                            "baked into the build" if leaked(settings.from_email)
                            else "not present"))

    # A contact from the database proves whether real people shipped with it.
    try:
        db = Database(settings.db_path)
        row = db.conn.execute(
            "SELECT email FROM contacts LIMIT 1").fetchone()
        db.close()
        if row:
            checks.append(Check("contact database",
                                FAIL if leaked(row["email"]) else OK,
                                "contacts are inside the build" if leaked(row["email"])
                                else "not present"))
    except Exception:
        pass

    if settings.resume_path.exists():
        head = settings.resume_path.read_bytes()[:2048]
        checks.append(Check("resume", FAIL if head in blob else OK,
                            "resume is inside the build" if head in blob
                            else "not present"))
    return checks
