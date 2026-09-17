"""Drafting ahead of time, and the send that clears up after itself.

Drafting used to happen once, at 08:25 on a weekday, which made a whole day's
sending depend on the laptop being awake for one particular minute. It now
runs all day against a depth target, so the common case is a run that finds
the queue full and does nothing. That "does nothing" has to be genuinely free:
rendering costs an LLM call per draft, so the decision has to come before the
template is even loaded.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src import pipeline as pipeline_module
from src.main import Pipeline


class CountingDB:
    def __init__(self, depth):
        self.depth = depth
        self.cleared = []

    def queued_draft_count(self):
        return self.depth

    def clear_draft_mirror(self, draft_id):
        self.cleared.append(draft_id)


def bare(depth=0, **settings):
    """A Pipeline with only the fields under test. No network, no database."""
    obj = object.__new__(Pipeline)
    obj.db = CountingDB(depth)
    obj.settings = SimpleNamespace(**settings)
    return obj


def test_a_full_queue_renders_nothing(capsys):
    # The template is never loaded, which is the point: the guard has to be
    # cheaper than the work it is avoiding.
    assert bare(depth=60).queue(limit=25, target=60) == 0
    assert "nothing to render" in capsys.readouterr().out


def test_a_queue_past_its_target_renders_nothing():
    assert bare(depth=99).queue(limit=25, target=60) == 0


def test_a_partly_full_queue_renders_only_the_shortfall(monkeypatch):
    seen = {}

    def fake_load(path):
        seen["loaded"] = True
        raise RuntimeError("stop here, the limit has already been computed")

    monkeypatch.setattr(pipeline_module, "load_template", fake_load)
    obj = bare(depth=55, template_path="config/template.txt")
    with pytest.raises(RuntimeError):
        obj.queue(limit=25, target=60)
    assert seen["loaded"]


def test_no_target_means_render_regardless(monkeypatch):
    def fake_load(path):
        raise RuntimeError("reached the render path")

    monkeypatch.setattr(pipeline_module, "load_template", fake_load)
    obj = bare(depth=500, template_path="config/template.txt")
    with pytest.raises(RuntimeError):
        obj.queue(limit=25, target=0)


# ------------------------------------------- clearing the Gmail copy after send

class FakeGmail:
    instances = []

    def __init__(self, settings):
        self.dropped = []
        self.settings = settings
        FakeGmail.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def drop(self, message_id):
        self.dropped.append(message_id)
        return True


@pytest.fixture(autouse=True)
def _reset():
    FakeGmail.instances = []


def test_a_sent_draft_has_its_gmail_copy_removed(monkeypatch):
    monkeypatch.setattr(pipeline_module, "GmailDrafts", FakeGmail)
    obj = bare(mirror_to_drafts=True)
    obj._drop_mirrors([(1, "<a@x>"), (2, "<b@x>")])
    assert FakeGmail.instances[0].dropped == ["<a@x>", "<b@x>"]
    assert obj.db.cleared == [1, 2]


def test_nothing_mirrored_opens_no_connection(monkeypatch):
    monkeypatch.setattr(pipeline_module, "GmailDrafts", FakeGmail)
    bare(mirror_to_drafts=True)._drop_mirrors([])
    assert FakeGmail.instances == []


def test_the_mirror_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(pipeline_module, "GmailDrafts", FakeGmail)
    bare(mirror_to_drafts=False)._drop_mirrors([(1, "<a@x>")])
    assert FakeGmail.instances == []


def test_a_failed_cleanup_does_not_fail_the_send(monkeypatch, capsys):
    # The mail has already gone out. Throwing here would fail a run that
    # actually succeeded, and the next sync drops the copy anyway.
    class Broken(FakeGmail):
        def __enter__(self):
            raise OSError("network down")

    monkeypatch.setattr(pipeline_module, "GmailDrafts", Broken)
    obj = bare(mirror_to_drafts=True)
    obj._drop_mirrors([(1, "<a@x>")])
    assert obj.db.cleared == []
    assert "left to the next sync" in capsys.readouterr().out
