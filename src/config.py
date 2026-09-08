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
    send_delay_seconds: int
    dry_run: bool
    send_window_start: dtime
    send_window_end: dtime
    send_timezone: str
    per_recipient_timezone: bool
    min_confidence: int
    followup_enabled: bool
    followup_after_days: int
    followup_max_total_sends: int
    followup_template_path: Path

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
            resume_path=_path("RESUME_PATH", "assets/resume.pdf"),
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
            send_delay_seconds=_int("SEND_DELAY_SECONDS", 20),
            dry_run=_bool("DRY_RUN", True),
            send_window_start=_clock("SEND_WINDOW_START", "08:30"),
            send_window_end=_clock("SEND_WINDOW_END", "10:00"),
            send_timezone=os.getenv("SEND_TIMEZONE", "America/Toronto").strip(),
            per_recipient_timezone=_bool("PER_RECIPIENT_TIMEZONE", True),
            min_confidence=_int("MIN_CONFIDENCE", 70),
            followup_enabled=_bool("FOLLOWUP_ENABLED", True),
            followup_after_days=_int("FOLLOWUP_AFTER_DAYS", 6),
            followup_max_total_sends=_int("FOLLOWUP_MAX_TOTAL_SENDS", 2),
            followup_template_path=_path(
                "FOLLOWUP_TEMPLATE_PATH", "config/followup.txt"),
        )

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
