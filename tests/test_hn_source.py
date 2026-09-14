"""Parsing Hacker News hiring comments, with no network access."""

from __future__ import annotations

import pytest

from src.hn_source import (
    CONSUMER, TOO_BIG, looks_ml, parse_comment, strip_html,
)

TARGETS = ("san francisco", "new york", "toronto")

TYPICAL = (
    "Acme AI | Senior ML Engineer | San Francisco, CA | ONSITE | full-time<p>"
    "We train large models in PyTorch. Email me at rishi@acme.ai"
)


def test_parses_the_standard_header():
    post = parse_comment(TYPICAL, TARGETS)
    assert post.company == "Acme AI"
    assert post.location == "San Francisco"
    assert post.emails == ["rishi@acme.ai"]
    assert post.domain == "acme.ai"
    assert post.is_ml


def test_strips_html_entities_and_tags():
    assert "<p>" not in strip_html("a<p>b")
    assert "&" in strip_html("a &amp; b")


def test_company_name_drops_a_trailing_url():
    post = parse_comment(
        "Devin ( https://experiencedevin.com ) | ML Eng | Toronto | "
        "pytorch work, mail morgan@experiencedevin.com", TARGETS)
    assert post.company == "Devin"
    assert post.domain == "experiencedevin.com"


def test_header_without_pipes_still_yields_a_name():
    post = parse_comment("Krea is hiring an LLM engineer in New York, "
                         "austin@krea.ai", TARGETS)
    assert post.company.startswith("Krea")
    assert post.domain == "krea.ai"


@pytest.mark.parametrize("text,expected", [
    ("we use PyTorch and transformers", True),
    ("hiring an LLM engineer", True),
    ("computer vision role", True),
    ("we need a Rails developer for our CRM", False),
    ("bookkeeping and payroll", False),
])
def test_ml_relevance(text, expected):
    assert looks_ml(text) is expected


def test_non_target_city_is_not_matched():
    post = parse_comment("Acme | ML Eng | Berlin | a@acme.ai pytorch", TARGETS)
    assert post.location == ""


def test_consumer_domain_is_not_taken_as_the_company_domain():
    """A gmail address tells us nothing about the company."""
    post = parse_comment(
        "Acme | ML | Toronto | pytorch | mail acmejobs@gmail.com "
        "or see https://acme.dev", TARGETS)
    assert post.domain == "acme.dev"
    assert "gmail.com" in CONSUMER


def test_company_domain_wins_over_a_consumer_one():
    post = parse_comment(
        "Acme | ML | Toronto | pytorch | me@gmail.com and hiring@acme.ai",
        TARGETS)
    assert post.domain == "acme.ai"


def test_comment_with_no_email_yields_none():
    post = parse_comment("Acme | ML Engineer | Toronto | apply on our site",
                         TARGETS)
    assert post.emails == []


# ---------------------------------------------------- reading a year of threads

class FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload


def test_recent_thread_ids_drops_the_neighbouring_monthly_threads(monkeypatch):
    """The same search returns 'who wants to be hired' and freelancer threads."""
    from src import hn_source

    hits = {"hits": [
        {"objectID": 1, "title": "Ask HN: Who is hiring? (September 2026)"},
        {"objectID": 2, "title": "Ask HN: Who wants to be hired? (September 2026)"},
        {"objectID": 3, "title": "Ask HN: Freelancer? Seeking freelancer?"},
        {"objectID": 4, "title": "Ask HN: Who is hiring? (August 2026)"},
    ]}
    monkeypatch.setattr(hn_source.requests, "get", lambda *a, **k: FakeResponse(hits))
    assert hn_source.recent_thread_ids(12) == ["1", "4"]
    assert hn_source.latest_thread_id() == "1"


def test_recent_thread_ids_honours_the_count(monkeypatch):
    from src import hn_source

    hits = {"hits": [{"objectID": i, "title": "Ask HN: Who is hiring?"}
                     for i in range(1, 10)]}
    monkeypatch.setattr(hn_source.requests, "get", lambda *a, **k: FakeResponse(hits))
    assert hn_source.recent_thread_ids(3) == ["1", "2", "3"]


def test_fetch_recent_posts_keeps_one_entry_per_company(monkeypatch):
    """A company that posts every month must not be queued twelve times."""
    from src import hn_source

    monkeypatch.setattr(hn_source, "recent_thread_ids", lambda *a, **k: ["t1", "t2"])
    by_thread = {
        "t1": [hn_source.HNPost(company="Acme", domain="acme.ai",
                                emails=["a@acme.ai"], location="Toronto")],
        "t2": [hn_source.HNPost(company="Acme", domain="acme.ai",
                                emails=["a@acme.ai"], location="Toronto"),
               hn_source.HNPost(company="Beta", domain="beta.ai",
                                emails=["b@beta.ai"], location="Toronto")],
    }
    monkeypatch.setattr(hn_source, "fetch_posts",
                        lambda targets, thread_id="", timeout=40: by_thread[thread_id])
    posts = hn_source.fetch_recent_posts(TARGETS, months=2)
    assert [p.domain for p in posts] == ["acme.ai", "beta.ai"]


def test_recent_thread_ids_survives_a_dead_api(monkeypatch):
    from src import hn_source

    def boom(*a, **k):
        raise RuntimeError("algolia down")

    monkeypatch.setattr(hn_source.requests, "get", boom)
    assert hn_source.recent_thread_ids(12) == []


def test_fetch_posts_drops_the_largest_employers(monkeypatch):
    """A year of threads surfaces Adobe and Google, which are not the target."""
    from src import hn_source

    comments = {"children": [
        {"text": "Adobe | ML Engineer | San Francisco | skatkar@adobe.com"},
        {"text": "Acme AI | ML Engineer | San Francisco | rishi@acme.ai"},
    ]}
    monkeypatch.setattr(hn_source.requests, "get",
                        lambda *a, **k: FakeResponse(comments))
    posts = hn_source.fetch_posts(TARGETS, thread_id="t1")
    assert [p.domain for p in posts] == ["acme.ai"]
    assert "adobe.com" in TOO_BIG
