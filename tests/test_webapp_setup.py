"""The setup console's reply template.

Drafting writes into the user's own mailbox, so it is opt-in: the console shows
no starter text, an empty box turns drafting off, and a template the scan could
not use is refused on save instead of being skipped silently every morning.
"""

from __future__ import annotations

import pytest

from src import webapp

REPLY = "Subject: Re: {original_subject}\n\nDear {first_name},\n\nThanks.\n"


@pytest.fixture
def install(tmp_path, monkeypatch):
    """A throwaway install directory, so no test touches the real config."""
    monkeypatch.setattr(webapp, "project_root", lambda: tmp_path)
    monkeypatch.setattr(webapp, "env_path", lambda: tmp_path / ".env")
    monkeypatch.setenv("REPLY_TEMPLATE_PATH", str(tmp_path / "config" / "reply.txt"))
    monkeypatch.setenv("TEMPLATE_PATH", str(tmp_path / "config" / "template.txt"))
    monkeypatch.setenv("RESUME_PATH", str(tmp_path / "assets" / "resume.pdf"))
    return tmp_path


def test_drafting_starts_off_with_no_starter_text(install):
    state = webapp.setup_state()
    assert state["values"]["REPLY_TEMPLATE"] == ""
    assert state["steps"]["replies"] == "warn"
    assert not (install / "config" / "reply.txt").exists()


def test_saving_a_reply_template_turns_drafting_on(install):
    webapp.apply_setup({"REPLY_TEMPLATE": REPLY})
    assert (install / "config" / "reply.txt").read_text(encoding="utf-8") == REPLY
    state = webapp.setup_state()
    assert state["values"]["REPLY_TEMPLATE"] == REPLY
    assert state["steps"]["replies"] == "ok"


def test_an_empty_box_turns_drafting_off(install):
    webapp.apply_setup({"REPLY_TEMPLATE": REPLY})
    webapp.apply_setup({"REPLY_TEMPLATE": "   \n"})
    assert not (install / "config" / "reply.txt").exists()


def test_a_save_without_the_field_leaves_the_template_alone(install):
    webapp.apply_setup({"REPLY_TEMPLATE": REPLY})
    webapp.apply_setup({"FROM_NAME": "Ada Lovelace"})
    assert (install / "config" / "reply.txt").exists()


@pytest.mark.parametrize("bad,why", [
    ("Dear {first_name},\n\nno subject line\n", "Subject:"),
    ("Subject: Re: hi\n\nDear {nickname},\n", "cannot fill"),
])
def test_an_unusable_template_is_refused_before_anything_is_written(install, bad, why):
    with pytest.raises(ValueError, match=why):
        webapp.apply_setup({"REPLY_TEMPLATE": bad, "FROM_NAME": "Ada Lovelace"})
    assert not (install / "config" / "reply.txt").exists()
    assert not (install / ".env").exists()
