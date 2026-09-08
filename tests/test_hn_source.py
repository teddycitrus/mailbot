"""Parsing Hacker News hiring comments, with no network access."""

from __future__ import annotations

import pytest

from src.hn_source import CONSUMER, looks_ml, parse_comment, strip_html

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
