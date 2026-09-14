"""The send-time completeness guard.

Queuing an unfinished draft is allowed so it can be previewed. Sending one is
not, however it came to be queued, including drafts written before a template
was finished.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.main import Pipeline


DRIVE_LINK = "https://drive.google.com/file/d/abc/view"


def pipeline(resume_path="assets/john_mannully_resume.pdf", resume_link=""):
    """A Pipeline with only the field _split_incomplete reads, no network or db."""
    obj = object.__new__(Pipeline)
    obj.settings = SimpleNamespace(resume_path=resume_path, resume_link=resume_link)
    return obj


def draft(subject="Hi Acme", body="Hello Stefan\n", email="a@acme.ai",
          attachments="assets/john_mannully_resume.pdf"):
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


def test_draft_carrying_the_resume_link_instead_of_a_pdf_passes():
    """The link is the fallback, so a draft using it is complete, not broken."""
    body = f"Hello Stefan\n[View my resume]({DRIVE_LINK})\n"
    ready, blocked = pipeline(resume_link=DRIVE_LINK)._split_incomplete(
        [draft(body=body, attachments="")]
    )
    assert len(ready) == 1 and blocked == []


def test_draft_with_neither_attachment_nor_link_is_still_blocked():
    ready, blocked = pipeline(resume_link=DRIVE_LINK)._split_incomplete(
        [draft(attachments="")]
    )
    assert ready == []
    assert blocked[0][1] == "no resume attached"


def test_deleted_attachment_is_allowed_through_when_a_link_can_replace_it(tmp_path):
    """_deliver swaps in the link; blocking here would drop the send entirely."""
    ready, blocked = pipeline(resume_link=DRIVE_LINK)._split_incomplete(
        [draft(attachments=str(tmp_path / "gone.pdf"))]
    )
    assert len(ready) == 1 and blocked == []


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
        daily_send_limit=25, send_ramp_enabled=False,
        dry_run=False, smtp_host="smtp.gmail.com",
        email_backend="smtp", smtp_port=587, smtp_user="u", smtp_pass="p",
        from_email="j@x.com", from_name="J", reply_to="", unsubscribe_mailto="j@x.com",
        resend_api_key="", send_delay_seconds=0, per_recipient_timezone=False,
        require_for_send=lambda: None, resume_link="",
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


# ------------------------------- the link steps in only when the PDF cannot


class _Recorder:
    """A backend that keeps the message instead of sending it."""

    name = "recorder"

    def __init__(self):
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def send(self, msg):
        from src.models import SendResult
        self.sent.append(msg)
        return SendResult(True, message_id="<1@x>", backend=self.name)


def _deliverable(tmp_path, attachment, resume_link):
    from src.database import Database
    from src.models import Company, Contact

    obj = object.__new__(Pipeline)
    obj.settings = SimpleNamespace(
        from_email="j@x.com", from_name="J", reply_to="",
        unsubscribe_mailto="j@x.com", send_delay_seconds=0,
        resume_path=str(tmp_path / "gone.pdf"), resume_link=resume_link,
    )
    obj.db = Database(tmp_path / "d.db")
    cid = obj.db.upsert_company(Company(name="Acme", domain="acme.ai"))
    ct = obj.db.upsert_contact(Contact(email="a@acme.ai", company_id=cid))
    obj.db.queue_draft(ct, "Subj", "Best,\nJohn\n", [attachment])
    return obj, obj.db.queued_drafts(limit=1)


def test_delivery_falls_back_to_the_link_when_the_pdf_has_gone(tmp_path, capsys):
    """The file vanished after queueing. Send the link rather than no resume."""
    obj, drafts = _deliverable(tmp_path, str(tmp_path / "gone.pdf"), DRIVE_LINK)
    backend = _Recorder()
    assert obj._deliver(backend, drafts, "2026-09-10") == 1
    msg = backend.sent[0]
    assert list(msg.iter_attachments()) == []
    body = msg.get_body(preferencelist=("plain",)).get_content()
    assert DRIVE_LINK in body
    assert "using the resume link" in capsys.readouterr().out
    obj.db.close()


def test_a_present_pdf_is_attached_and_the_link_stays_out(tmp_path):
    pdf = tmp_path / "john_mannully_resume.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    obj, drafts = _deliverable(tmp_path, str(pdf), DRIVE_LINK)
    backend = _Recorder()
    assert obj._deliver(backend, drafts, "2026-09-10") == 1
    msg = backend.sent[0]
    assert [a.get_filename() for a in msg.iter_attachments()] == [
        "john_mannully_resume.pdf"]
    assert DRIVE_LINK not in msg.as_string()
    obj.db.close()


def test_without_a_link_a_lost_pdf_stops_the_send(tmp_path, capsys):
    """No resume in any form is not a message worth sending."""
    obj, drafts = _deliverable(tmp_path, str(tmp_path / "gone.pdf"), "")
    backend = _Recorder()
    assert obj._deliver(backend, drafts, "2026-09-10") == 0
    assert backend.sent == []
    assert "BLOCKED" in capsys.readouterr().out
    obj.db.close()
