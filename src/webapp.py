"""Local web console: setup first, progress dashboard once configured.

Serves the built React bundle plus a small JSON API from the standard library
only, so the packaged executable needs no web framework and no network access.
It binds to loopback and is not reachable from other machines.

Everything personal lives on the machine running it. The build that ships
contains no credentials, no resume and no contact database; the first thing a
new install shows is the setup console that asks for them.
"""

from __future__ import annotations

import json
import mimetypes
import re
import sqlite3
import threading
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import Settings, app_root
from .database import Database
from .doctor import run_checks

# Values that must never be echoed back to the browser in clear text.
SECRET_KEYS = {"SMTP_PASS", "IMAP_PASS", "GROQ_API_KEY", "GITHUB_TOKEN", "RESEND_API_KEY"}

# Written to .env; anything else the form sends is ignored.
ALLOWED_KEYS = SECRET_KEYS | {
    "FROM_NAME", "FROM_EMAIL", "REPLY_TO", "UNSUBSCRIBE_MAILTO",
    "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "IMAP_HOST", "IMAP_PORT", "IMAP_USER",
    "TARGET_LOCATIONS", "DAILY_SEND_LIMIT", "EMAIL_BACKEND", "GROQ_MODEL",
}

STARTER_TEMPLATE = """Subject: Quick note about {company}

Hi {first_name},

{personal_note}

Replace this paragraph with two or three sentences about who you are and what
you are looking for. Keep it short; this is a cold email.

My resume is attached. If you are hiring, I would welcome a short conversation.

Best,
{sender_name}
{sender_email}

Not interested? Reply to this message or write to {unsubscribe} and I will not
contact you again.
"""


def project_root() -> Path:
    """Where this install keeps its data. See config.app_root."""
    return app_root()


def env_path() -> Path:
    return project_root() / ".env"


def read_env() -> dict[str, str]:
    path = env_path()
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def write_env(updates: dict[str, str]) -> None:
    """Merge into .env, preserving comments and any keys we do not manage."""
    path = env_path()
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    out: list[str] = []
    for line in existing:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
                continue
        out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}")
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def setup_state() -> dict[str, Any]:
    """What is configured, what is missing, and the safe-to-show values."""
    env = read_env()
    root = project_root()
    settings = Settings.load(env_path() if env_path().exists() else None)

    aspects_file = root / "config" / "aspects.txt"
    template_file = settings.template_path
    resume = settings.resume_path

    aspects = aspects_file.read_text(encoding="utf-8").strip() if aspects_file.exists() else ""
    template = template_file.read_text(encoding="utf-8") if template_file.exists() else STARTER_TEMPLATE

    def flag(done: bool, optional: bool = False) -> str:
        return "ok" if done else ("warn" if optional else "todo")

    has_identity = bool(env.get("FROM_NAME") and env.get("FROM_EMAIL"))
    has_creds = bool(env.get("SMTP_PASS"))
    has_resume = resume.exists() and resume.stat().st_size > 1000
    has_aspects = len([l for l in aspects.splitlines() if l.strip()]) >= 2
    has_template = bool(template.strip()) and template.lower().startswith("subject:")
    has_optional = bool(env.get("GROQ_API_KEY") or env.get("GITHUB_TOKEN"))
    has_targeting = bool(env.get("TARGET_LOCATIONS") and env.get("DAILY_SEND_LIMIT"))

    values = {k: v for k, v in env.items() if k in ALLOWED_KEYS and k not in SECRET_KEYS}
    # Report secrets as set or unset, never their contents.
    for key in SECRET_KEYS:
        values[key] = "********" if env.get(key) else ""
    values["ASPECTS"] = aspects
    values["TEMPLATE"] = template

    return {
        "configured": all([has_identity, has_creds, has_resume, has_aspects, has_template]),
        "steps": {
            "identity": flag(has_identity),
            "credentials": flag(has_creds),
            "resume": flag(has_resume),
            "aspects": flag(has_aspects),
            "template": flag(has_template),
            "optional": flag(has_optional, optional=True),
            "targeting": flag(has_targeting, optional=True),
        },
        "values": values,
        "resumeName": resume.name if has_resume else "",
    }


def apply_setup(payload: dict[str, str]) -> None:
    root = project_root()
    updates: dict[str, str] = {}
    for key, value in payload.items():
        if key not in ALLOWED_KEYS:
            continue
        # A masked secret means "leave it alone", not "set it to asterisks".
        if key in SECRET_KEYS and set(value.strip()) == {"*"}:
            continue
        updates[key] = value.strip().replace("\n", " ")

    email = updates.get("FROM_EMAIL", "").strip()
    if email:
        # These three are the same address in every realistic setup, so fill
        # them rather than asking the same question three times.
        updates.setdefault("REPLY_TO", email)
        updates.setdefault("UNSUBSCRIBE_MAILTO", email)
        updates.setdefault("SMTP_USER", email)
        updates.setdefault("IMAP_USER", email)
    if updates.get("SMTP_PASS") and not read_env().get("IMAP_PASS"):
        updates["IMAP_PASS"] = updates["SMTP_PASS"]
    if updates:
        write_env(updates)

    (root / "config").mkdir(exist_ok=True)
    if "ASPECTS" in payload:
        lines = [l.strip() for l in payload["ASPECTS"].splitlines() if l.strip()]
        (root / "config" / "aspects.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if "TEMPLATE" in payload and payload["TEMPLATE"].strip():
        settings = Settings.load(env_path() if env_path().exists() else None)
        settings.template_path.parent.mkdir(parents=True, exist_ok=True)
        settings.template_path.write_text(payload["TEMPLATE"], encoding="utf-8")


# ------------------------------------------------------------- dashboard

def _pct_change(now: int, before: int) -> tuple[float | None, str]:
    if before == 0:
        return (None, "flat") if now == 0 else (100.0, "up")
    change = (now - before) / before * 100.0
    return change, "up" if change > 1 else "down" if change < -1 else "flat"


def dashboard_data(db: Database, settings: Settings, days: int = 14) -> dict[str, Any]:
    con = db.conn
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=days)).isoformat()
    prev_start = (now - timedelta(days=days * 2)).isoformat()

    def count(sql: str, args: tuple = ()) -> int:
        return int(con.execute(sql, args).fetchone()[0])

    sent = count("SELECT COUNT(*) FROM sends WHERE status='sent' AND sent_at>=?", (start,))
    sent_prev = count(
        "SELECT COUNT(*) FROM sends WHERE status='sent' AND sent_at>=? AND sent_at<?",
        (prev_start, start))
    replies = count("SELECT COUNT(*) FROM events WHERE kind='reply' AND created_at>=?", (start,))
    replies_prev = count(
        "SELECT COUNT(*) FROM events WHERE kind='reply' AND created_at>=? AND created_at<?",
        (prev_start, start))
    bounces = count("SELECT COUNT(*) FROM events WHERE kind='bounce' AND created_at>=?", (start,))
    bounces_prev = count(
        "SELECT COUNT(*) FROM events WHERE kind='bounce' AND created_at>=? AND created_at<?",
        (prev_start, start))
    queued = count("SELECT COUNT(*) FROM drafts WHERE status='queued'")
    contacts = count("SELECT COUNT(*) FROM contacts")

    sent_pct, sent_trend = _pct_change(sent, sent_prev)
    rep_pct, rep_trend = _pct_change(replies, replies_prev)
    bnc_pct, bnc_trend = _pct_change(bounces, bounces_prev)

    rate = (replies / sent * 100) if sent else 0.0

    metrics = [
        {"id": "sent", "label": f"Sent ({days}d)", "value": sent, "changePct": sent_pct,
         "trend": sent_trend, "higherIsBetter": True,
         "hint": f"cap {settings.daily_send_limit}/day"},
        {"id": "replies", "label": "Replies", "value": replies, "changePct": rep_pct,
         "trend": rep_trend, "higherIsBetter": True,
         "hint": f"{rate:.0f}% reply rate"},
        {"id": "bounces", "label": "Bounces", "value": bounces, "changePct": bnc_pct,
         "trend": bnc_trend, "higherIsBetter": False,
         "hint": "keep under 5%"},
        {"id": "queued", "label": "Queued", "value": queued, "changePct": None,
         "trend": "flat", "higherIsBetter": True,
         "hint": f"{contacts} contacts known"},
    ]

    series = []
    for offset in range(days - 1, -1, -1):
        day = (now - timedelta(days=offset)).date().isoformat()
        series.append({
            "date": day,
            "sent": count("SELECT COUNT(*) FROM sends WHERE status='sent' AND local_date=?", (day,)),
            "replies": count(
                "SELECT COUNT(*) FROM events WHERE kind='reply' AND substr(created_at,1,10)=?", (day,)),
            "bounces": count(
                "SELECT COUNT(*) FROM events WHERE kind='bounce' AND substr(created_at,1,10)=?", (day,)),
        })

    sources = [
        {"source": row["source"] or "unknown", "count": int(row["n"])}
        for row in con.execute(
            "SELECT co.source AS source, COUNT(*) AS n FROM contacts c "
            "LEFT JOIN companies co ON co.id=c.company_id GROUP BY co.source ORDER BY n DESC")
    ]

    rows = []
    for row in con.execute(
        """SELECT c.id, c.email, c.first_name, c.last_name, c.confidence, c.verify_status,
                  c.source, c.status, c.created_at,
                  co.name AS company, co.location
           FROM contacts c LEFT JOIN companies co ON co.id=c.company_id
           ORDER BY c.confidence DESC, c.id DESC LIMIT 500"""
    ):
        rows.append({
            "id": int(row["id"]),
            "email": row["email"],
            "name": " ".join(p for p in (row["first_name"], row["last_name"]) if p),
            "company": row["company"] or "",
            "location": (row["location"] or "").split(",")[0],
            "confidence": int(row["confidence"]),
            "verify": row["verify_status"],
            "source": row["source"],
            "status": row["status"],
            "createdAt": row["created_at"],
        })

    return {
        "generatedAt": now.isoformat(timespec="seconds"),
        "metrics": metrics,
        "series": series,
        "sources": sources,
        "contacts": rows,
    }


# ------------------------------------------------------------ http layer

def static_root() -> Path:
    """Where the built bundle lives, in dev and inside a frozen executable."""
    import sys
    if getattr(sys, "frozen", False):          # PyInstaller one-file bundle
        return Path(sys._MEIPASS) / "dashboard"  # type: ignore[attr-defined]
    return project_root() / "dashboard" / "dist"


FILENAME_RE = re.compile(rb'filename="([^"]*)"')


def uploaded_filename(part: bytes) -> str:
    """The client's own name for an uploaded file, reduced to a safe PDF name.

    The recipient sees this on the attachment, so a resume that arrives as
    john_mannully_resume.pdf should stay that rather than becoming resume.pdf.
    Anything outside a conservative character set is replaced, and a name that
    is not a plain .pdf is rejected so this cannot write outside assets/.
    """
    match = FILENAME_RE.search(part.split(b"\r\n\r\n")[0])
    if not match:
        return ""
    raw = match.group(1).decode("utf-8", "replace")
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return name if name.lower().endswith(".pdf") and len(name) > 4 else ""


class Handler(BaseHTTPRequestHandler):
    server_version = "mailbot"

    def log_message(self, *_args) -> None:
        pass  # the console is the UI; request logs would only clutter it

    # ---------- helpers ----------

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # Nothing here should ever be embedded elsewhere or cached stale.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json")

    def _error(self, message: str, status: int = 400) -> None:
        self._send(status, message.encode(), "text/plain; charset=utf-8")

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    # ---------- routes ----------

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/api/setup":
                return self._json(setup_state())
            if path == "/api/health":
                settings = Settings.load(env_path() if env_path().exists() else None)
                checks = run_checks(settings)
                return self._json({"checks": [
                    {"name": c.name, "state": c.state, "detail": c.detail} for c in checks
                ]})
            if path == "/api/dashboard":
                settings = Settings.load(env_path() if env_path().exists() else None)
                db = Database(settings.db_path)
                try:
                    return self._json(dashboard_data(db, settings))
                finally:
                    db.close()
            if path.startswith("/api/"):
                return self._error("no such endpoint", 404)
        except sqlite3.Error as exc:
            return self._error(f"database error: {exc}", 500)
        except Exception as exc:  # a dead console is worse than a shown error
            return self._error(f"{type(exc).__name__}: {exc}", 500)
        return self._serve_static(path)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/api/setup":
                payload = json.loads(self._body() or b"{}")
                if not isinstance(payload, dict):
                    return self._error("expected an object")
                apply_setup(payload)
                return self._json(setup_state())
            if path == "/api/resume":
                return self._save_resume()
            return self._error("no such endpoint", 404)
        except json.JSONDecodeError:
            return self._error("malformed JSON")
        except Exception as exc:
            return self._error(f"{type(exc).__name__}: {exc}", 500)

    # ---------- static + upload ----------

    def _serve_static(self, path: str) -> None:
        root = static_root()
        if not root.exists():
            return self._error(
                "dashboard bundle missing. Run: cd dashboard && npm install && npm run build",
                500)
        rel = path.lstrip("/") or "index.html"
        target = (root / rel).resolve()
        if not str(target).startswith(str(root.resolve())):
            return self._error("forbidden", 403)   # no traversal out of the bundle
        if not target.is_file():
            target = root / "index.html"           # single page app fallback
        ctype, _ = mimetypes.guess_type(target.name)
        self._send(200, target.read_bytes(), ctype or "application/octet-stream")

    def _save_resume(self) -> None:
        """Accept a single multipart PDF without pulling in a parser library."""
        ctype = self.headers.get("Content-Type", "")
        match = re.search(r"boundary=([^;]+)", ctype)
        if not match:
            return self._error("expected multipart/form-data")
        boundary = match.group(1).strip('"').encode()
        raw = self._body()
        if len(raw) > 15 * 1024 * 1024:
            return self._error("file too large")
        part = next(
            (p for p in raw.split(b"--" + boundary) if b"filename=" in p.split(b"\r\n\r\n")[0]),
            None,
        )
        if part is None:
            return self._error("no file in request")
        content = part.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n", 1)[0]
        if not content.startswith(b"%PDF-"):
            return self._error("that is not a PDF")
        settings = Settings.load(env_path() if env_path().exists() else None)
        # Keep the name the file arrived with. It is what the recipient sees on
        # the attachment, and "john_mannully_resume.pdf" reads better in their
        # downloads folder than whatever placeholder the config shipped with.
        target = settings.resume_path
        uploaded = uploaded_filename(part)
        if uploaded and uploaded != target.name:
            new_target = target.parent / uploaded
            # Prune the previous resume only inside the app's own assets
            # folder. RESUME_PATH can point anywhere, and deleting every PDF
            # in someone's Documents folder is not this endpoint's business.
            if target.parent.resolve() == (project_root() / "assets").resolve():
                for stale in target.parent.glob("*.pdf"):
                    if stale.resolve() != new_target.resolve():
                        stale.unlink()
            target = new_target
            write_env({"RESUME_PATH": str(
                target.relative_to(project_root())
                if target.is_relative_to(project_root()) else target
            ).replace("\\", "/")})
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return self._json(setup_state())


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    """Run the console until interrupted. Loopback only, by design."""
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"console at {url}   (ctrl-c to stop)")
    if not static_root().exists():
        print("warning: dashboard not built yet -> cd dashboard && npm install && npm run build")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
