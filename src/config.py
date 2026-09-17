"""Environment-backed configuration.

Every knob lives in .env (see .env.example). Nothing here touches the network,
so importing this module is always safe.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import time as dtime
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # dotenv is optional; plain env vars still work.
    def load_dotenv(*_args, **_kwargs):
        return False


def app_root() -> Path:
    """The directory this install reads and writes its data in.

    A frozen build unpacks its code to a temp directory that Windows deletes on
    exit, so data must live beside the executable instead. In a source checkout
    it is the project directory.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _path(name: str, default: str) -> Path:
    """Resolve a configured path against the install directory, not the cwd."""
    raw = Path(os.getenv(name, "").strip() or default)
    return raw if raw.is_absolute() else app_root() / raw


class ConfigError(RuntimeError):
    """Raised when required settings are missing for the requested action."""


def _split(value: str) -> tuple[str, ...]:
    return tuple(item.strip().lower() for item in value.split(",") if item.strip())


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _clock(name: str, default: str) -> dtime:
    raw = os.getenv(name, "").strip() or default
    try:
        hour, minute = (int(part) for part in raw.split(":", 1))
        return dtime(hour, minute)
    except ValueError as exc:
        raise ConfigError(f"{name} must look like HH:MM, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    # Enrichment (all free tier)
    groq_api_key: str
    groq_model: str
    http_rate_limit_per_min: int
    http_max_fetches_per_run: int
    groq_max_calls_per_run: int
    github_token: str
    github_max_calls_per_run: int
    scrape_user_agent: str

    # Delivery
    email_backend: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_pass: str
    resend_api_key: str

    # Identity
    from_email: str
    from_name: str
    reply_to: str
    unsubscribe_mailto: str

    # Bounce scanning
    imap_host: str
    imap_port: int
    imap_user: str
    imap_pass: str

    # Paths
    db_path: Path
    template_path: Path
    resume_path: Path
    # Hosted copy of the same resume. Used only when the PDF cannot be
    # attached, never alongside it: a recipient who sees an attachment and
    # a Drive link in the same message reads it as two documents.
    resume_link: str

    # Targeting
    target_locations: tuple[str, ...]
    target_industries: tuple[str, ...]
    max_company_age_years: int
    max_employees: int
    target_roles: tuple[str, ...]
    hiring_only: bool
    prefer_recently_funded: bool

    # Guardrails
    daily_send_limit: int
    send_ramp_enabled: bool
    send_ramp_start: int
    send_ramp_step: int
    send_ramp_max_bounce_pct: int
    send_delay_seconds: int
    dry_run: bool
    send_window_start: dtime
    send_window_end: dtime
    send_timezone: str
    per_recipient_timezone: bool
    min_confidence: int
    # How deep the queue is kept. Drafting runs all day and stops once this
    # many are waiting, so the morning never depends on a render having
    # happened that morning, and a laptop that sleeps through a day costs
    # nothing: the drafts were ready the night before.
    queue_target: int
    # Copy every queued draft into the Gmail Drafts folder, so a day is not
    # lost when this machine is closed. See mirror.py.
    mirror_to_drafts: bool
    followup_enabled: bool
    followup_after_days: int
    followup_max_total_sends: int
    followup_template_path: Path
    # Answer drafted for each reply and left in Drafts, never sent. Drafting
    # is off when this file does not exist.
    reply_template_path: Path
    # Companies named by hand, which skip the size and age gates and sort
    # ahead of everything else. See priority.py.
    priority_path: Path

    @classmethod
    def load(cls, env_file: str | os.PathLike[str] | None = ".env") -> "Settings":
        if env_file and Path(env_file).exists():
            load_dotenv(env_file, override=False)
        return cls(
            groq_api_key=os.getenv("GROQ_API_KEY", "").strip(),
            groq_model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip(),
            http_rate_limit_per_min=_int("HTTP_RATE_LIMIT_PER_MIN", 30),
            http_max_fetches_per_run=_int("HTTP_MAX_FETCHES_PER_RUN", 400),
            groq_max_calls_per_run=_int("GROQ_MAX_CALLS_PER_RUN", 40),
            github_token=os.getenv("GITHUB_TOKEN", "").strip(),
            github_max_calls_per_run=_int("GITHUB_MAX_CALLS_PER_RUN", 50),
            scrape_user_agent=os.getenv(
                "SCRAPE_USER_AGENT",
                "Mozilla/5.0 (compatible; mailbot/0.1; +job-search-outreach)",
            ).strip(),
            email_backend=os.getenv("EMAIL_BACKEND", "smtp").strip().lower(),
            smtp_host=os.getenv("SMTP_HOST", "").strip(),
            smtp_port=_int("SMTP_PORT", 587),
            smtp_user=os.getenv("SMTP_USER", "").strip(),
            smtp_pass=os.getenv("SMTP_PASS", ""),
            resend_api_key=os.getenv("RESEND_API_KEY", "").strip(),
            from_email=os.getenv("FROM_EMAIL", "").strip(),
            from_name=os.getenv("FROM_NAME", "").strip(),
            reply_to=os.getenv("REPLY_TO", "").strip(),
            unsubscribe_mailto=os.getenv("UNSUBSCRIBE_MAILTO", "").strip(),
            imap_host=os.getenv("IMAP_HOST", "").strip(),
            imap_port=_int("IMAP_PORT", 993),
            imap_user=os.getenv("IMAP_USER", "").strip(),
            imap_pass=os.getenv("IMAP_PASS", ""),
            db_path=_path("DB_PATH", "outreach.db"),
            template_path=_path("TEMPLATE_PATH", "config/template.txt"),
            resume_path=_path("RESUME_PATH", "assets/john_mannully_resume.pdf"),
            resume_link=os.getenv("RESUME_LINK", "").strip(),
            target_locations=_split(
                os.getenv("TARGET_LOCATIONS", "San Francisco,New York,Toronto")
            ),
            target_industries=_split(
                os.getenv("TARGET_INDUSTRIES", "machine learning,artificial intelligence")
            ),
            max_company_age_years=_int("MAX_COMPANY_AGE_YEARS", 5),
            max_employees=_int("MAX_EMPLOYEES", 200),
            target_roles=_split(
                os.getenv("TARGET_ROLES", "founder,co-founder,cto,lead engineer,ml engineer")
            ),
            hiring_only=_bool("HIRING_ONLY", True),
            prefer_recently_funded=_bool("PREFER_RECENTLY_FUNDED", True),
            daily_send_limit=_int("DAILY_SEND_LIMIT", 25),
            send_ramp_enabled=_bool("SEND_RAMP_ENABLED", True),
            send_ramp_start=_int("SEND_RAMP_START", 8),
            send_ramp_step=_int("SEND_RAMP_STEP", 2),
            send_ramp_max_bounce_pct=_int("SEND_RAMP_MAX_BOUNCE_PCT", 5),
            send_delay_seconds=_int("SEND_DELAY_SECONDS", 20),
            dry_run=_bool("DRY_RUN", True),
            send_window_start=_clock("SEND_WINDOW_START", "08:30"),
            send_window_end=_clock("SEND_WINDOW_END", "10:00"),
            send_timezone=os.getenv("SEND_TIMEZONE", "America/Toronto").strip(),
            per_recipient_timezone=_bool("PER_RECIPIENT_TIMEZONE", True),
            min_confidence=_int("MIN_CONFIDENCE", 70),
            queue_target=_int("QUEUE_TARGET", 60),
            mirror_to_drafts=_bool("MIRROR_TO_DRAFTS", True),
            followup_enabled=_bool("FOLLOWUP_ENABLED", True),
            followup_after_days=_int("FOLLOWUP_AFTER_DAYS", 6),
            followup_max_total_sends=_int("FOLLOWUP_MAX_TOTAL_SENDS", 2),
            followup_template_path=_path(
                "FOLLOWUP_TEMPLATE_PATH", "config/followup.txt"),
            reply_template_path=_path("REPLY_TEMPLATE_PATH", "config/reply.txt"),
            priority_path=_path("PRIORITY_PATH", "config/priority.txt"),
        )

    def daily_cap(self, active_days: int, bounce_pct: float) -> tuple[int, str]:
        """Today's cap, grown from SEND_RAMP_START toward DAILY_SEND_LIMIT.

        A young sending address that opens at full daily volume reads as a
        blast, because mailbox providers judge a sender on how its volume
        grows as much as on the volume itself. So the cap starts small and
        earns one step per day actually sent on, never past DAILY_SEND_LIMIT,
        which stays the hard ceiling.

        `active_days` counts days sent on *before* today, so the cap holds
        still for the whole of today instead of climbing the moment the
        first message goes out.

        Returns the cap and a short reason, because a cap that silently
        disagrees with DAILY_SEND_LIMIT is otherwise baffling in the logs.
        """
        ceiling = max(0, self.daily_send_limit)
        if not self.send_ramp_enabled:
            return ceiling, "ramp off"

        earned = self.send_ramp_start + self.send_ramp_step * max(0, active_days)
        cap = max(0, min(ceiling, earned))

        # Climbing while mail is bouncing is what gets an address blocked, so
        # fall back to the opening step until the bounce rate recovers.
        if bounce_pct > self.send_ramp_max_bounce_pct:
            held = max(0, min(cap, self.send_ramp_start))
            return held, (f"held at {held}, bounce rate {bounce_pct:.1f}% "
                          f"over {self.send_ramp_max_bounce_pct}%")

        if cap >= ceiling:
            return ceiling, f"ramp complete at {ceiling}/day"
        return cap, f"ramp day {active_days + 1}, climbing to {ceiling}"

    def require(self, *names: str) -> None:
        """Fail loudly and all at once rather than midway through a run."""
        missing = [name for name in names if not getattr(self, name)]
        if missing:
            keys = ", ".join(name.upper() for name in missing)
            raise ConfigError(f"missing required settings in .env: {keys}")

    def require_for_send(self) -> None:
        self.require("from_email", "from_name", "unsubscribe_mailto")
        if self.email_backend == "smtp":
            self.require("smtp_host", "smtp_user", "smtp_pass")
        elif self.email_backend == "resend":
            self.require("resend_api_key")
        else:
            raise ConfigError(
                f"EMAIL_BACKEND must be 'smtp' or 'resend', got {self.email_backend!r}"
            )

    def require_for_bounces(self) -> None:
        self.require("imap_host", "imap_user", "imap_pass")
