"""The send-time completeness guard.

Queuing an unfinished draft is allowed so it can be previewed. Sending one is
not, however it came to be queued, including drafts written before a template
was finished.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.main import Pipeline


def pipeline(resume_path="assets/resume.pdf"):
    """A Pipeline with only the field _split_incomplete reads, no network or db."""
    obj = object.__new__(Pipeline)
    obj.settings = SimpleNamespace(resume_path=resume_path)
    return obj


def draft(subject="Hi Acme", body="Hello Stefan\n", email="a@acme.ai",
          attachments="assets/resume.pdf"):
    return {"subject": subject, "body": body, "email": email,
            "attachments": attachments}


def test_complete_draft_passes(tmp_path):
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    ready, blocked = pipeline(str(pdf))._split_incomplete(
        [draft(attachments=str(pdf))]
    )
    assert len(ready) == 1 and blocked == []


def test_unedited_body_is_blocked(tmp_path):
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    ready, blocked = pipeline(str(pdf))._split_incomplete(
        [draft(body="I work on [YOUR FOCUS AREA]\n", attachments=str(pdf))]
    )
    assert ready == []
    assert "unedited template" in blocked[0][1]


def test_unedited_subject_is_blocked(tmp_path):
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    ready, blocked = pipeline(str(pdf))._split_incomplete(
        [draft(subject="Hi [COMPANY NAME HERE]", attachments=str(pdf))]
    )
    assert ready == [] and blocked


def test_missing_attachment_entry_is_blocked():
    ready, blocked = pipeline()._split_incomplete([draft(attachments="")])
    assert ready == []
    assert blocked[0][1] == "no resume attached"


def test_attachment_pointing_at_a_deleted_file_is_blocked(tmp_path):
    ready, blocked = pipeline()._split_incomplete(
        [draft(attachments=str(tmp_path / "gone.pdf"))]
    )
    assert ready == []
    assert "missing" in blocked[0][1]


def test_mixed_batch_splits_correctly(tmp_path):
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    good = draft(email="good@acme.ai", attachments=str(pdf))
    bad = draft(email="bad@acme.ai", body="[STILL A HOLE]\n", attachments=str(pdf))
    ready, blocked = pipeline(str(pdf))._split_incomplete([good, bad])
    assert [d["email"] for d in ready] == ["good@acme.ai"]
    assert [b[0] for b in blocked] == ["bad@acme.ai"]


# ------------------------------------------- connection failures stay legible

import smtplib  # noqa: E402
from types import SimpleNamespace as NS  # noqa: E402


def _pipeline_for_send(tmp_path, monkeypatch, raises):
    """A Pipeline whose delivery step raises, to check error reporting."""
    from src.database import Database
    from src.models import Company, Contact

    obj = object.__new__(Pipeline)
    obj.settings = NS(
        resume_path=str(tmp_path / "r.pdf"), send_timezone="America/Toronto",
        send_window_start=__import__("datetime").time(0, 0),
        send_window_end=__import__("datetime").time(23, 59),
        daily_send_limit=25, dry_run=False, smtp_host="smtp.gmail.com",
        email_backend="smtp", smtp_port=587, smtp_user="u", smtp_pass="p",
        from_email="j@x.com", from_name="J", reply_to="", unsubscribe_mailto="j@x.com",
        resend_api_key="", send_delay_seconds=0, per_recipient_timezone=False,
        require_for_send=lambda: None,
    )
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    obj.db = Database(tmp_path / "s.db")
    cid = obj.db.upsert_company(Company(name="Acme", domain="acme.ai"))
    ct = obj.db.upsert_contact(Contact(email="a@acme.ai", company_id=cid))
    obj.db.queue_draft(ct, "Subj", "Body\n", [str(pdf)])

    def boom(*_a, **_k):
        raise raises

    monkeypatch.setattr(Pipeline, "_deliver", boom)
    return obj


def test_bad_app_password_reports_cleanly(tmp_path, monkeypatch, capsys):
    err = smtplib.SMTPAuthenticationError(535, b"BadCredentials")
    p = _pipeline_for_send(tmp_path, monkeypatch, err)
    assert p.send(limit=1, ignore_window=True) == 0
    out = capsys.readouterr().out
    assert "app password" in out
    assert "Traceback" not in out
    p.db.close()


def test_unreachable_server_reports_cleanly(tmp_path, monkeypatch, capsys):
    p = _pipeline_for_send(tmp_path, monkeypatch, OSError("network down"))
    assert p.send(limit=1, ignore_window=True) == 0
    out = capsys.readouterr().out
    assert "could not reach" in out
    p.db.close()
