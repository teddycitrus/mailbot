"""Target-criteria filtering and the YC page parser, with no network access."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.finder import batch_year, domain_from_url, qualifies, role_matches
from src.scraper import extract_emails, parse_yc_founders, split_name

NOW = 2026

SETTINGS = SimpleNamespace(
    target_locations=("san francisco", "new york", "toronto"),
    max_employees=200,
    max_company_age_years=5,
    hiring_only=True,
    target_roles=("founder", "co-founder", "cto", "lead engineer", "ml engineer"),
)


def record(**over):
    base = dict(
        status="Active",
        website="https://acme.ai",
        all_locations="San Francisco, CA, USA",
        team_size=25,
        batch="Winter 2024",
        isHiring=True,
    )
    base.update(over)
    return base


def test_a_good_record_qualifies():
    ok, why = qualifies(record(), SETTINGS, NOW)
    assert ok and why == ""


@pytest.mark.parametrize("over,expect", [
    ({"status": "Inactive"}, "inactive"),
    ({"website": ""}, "no website"),
    ({"all_locations": "Berlin, Germany"}, "location"),
    ({"team_size": 500}, "team_size"),
    ({"team_size": 0}, "team_size"),
    ({"batch": "Winter 2015"}, "too old"),
    ({"batch": ""}, "unknown batch"),
    ({"isHiring": False}, "not hiring"),
])
def test_disqualifying_conditions(over, expect):
    ok, why = qualifies(record(**over), SETTINGS, NOW)
    assert not ok
    assert expect in why


def test_age_boundary_is_inclusive():
    assert qualifies(record(batch="Summer 2021"), SETTINGS, NOW)[0]
    assert not qualifies(record(batch="Summer 2020"), SETTINGS, NOW)[0]


def test_employee_boundary_is_inclusive():
    assert qualifies(record(team_size=200), SETTINGS, NOW)[0]
    assert not qualifies(record(team_size=201), SETTINGS, NOW)[0]


def test_hiring_filter_can_be_relaxed():
    relaxed = SimpleNamespace(**{**SETTINGS.__dict__, "hiring_only": False})
    assert qualifies(record(isHiring=False), relaxed, NOW)[0]


@pytest.mark.parametrize("url,expected", [
    ("https://www.acme.ai/", "acme.ai"),
    ("http://acme.ai", "acme.ai"),
    ("acme.ai", "acme.ai"),
    ("https://usepylon.com/?utm_source=x", "usepylon.com"),
])
def test_domain_extraction(url, expected):
    assert domain_from_url(url) == expected


@pytest.mark.parametrize("batch,year", [
    ("Winter 2024", 2024), ("Summer 2021", 2021), ("", 0), ("unspecified", 0),
])
def test_batch_year(batch, year):
    assert batch_year(batch) == year


def test_split_name():
    assert split_name("Thomas Stewart") == ("Thomas", "Stewart")
    assert split_name("Som Ranjan Mohapatra") == ("Som", "Mohapatra")
    assert split_name("Cher") == ("Cher", "")
    assert split_name("  ") == ("", "")


def test_role_matching_reads_the_bio_too():
    person = SimpleNamespace(role="Founder", bio="CTO @ Acme, ex-Google")
    assert role_matches(person, ("cto",))
    unrelated = SimpleNamespace(role="Office Manager", bio="keeps the lights on")
    assert not role_matches(unrelated, SETTINGS.target_roles)


# ---------------------------------------------------------- page parsing

YC_HTML = """
<html><body>
<div>Active Founders</div>
<div><div>
  <div>Thomas Stewart</div><div>Founder</div>
  <div>CEO @ Hadrius (W23). 2x founder including 1 exit.</div>
  <a href="https://www.linkedin.com/in/thomasjstewart">in</a>
</div></div>
<div><div>
  <div>Allen Calderwood</div><div>Founder</div>
  <div>CTO @ Hadrius (W23), Quantbase, ex-Google, ex-Chime</div>
  <a href="https://www.linkedin.com/in/androidallen">in</a>
</div></div>
</body></html>
"""


def test_parses_founders_with_roles_and_bios():
    people = parse_yc_founders(YC_HTML)
    names = sorted(p.full_name for p in people)
    assert names == ["Allen Calderwood", "Thomas Stewart"]
    allen = next(p for p in people if p.first_name == "Allen")
    assert "CTO" in allen.bio
    assert "linkedin.com/in/androidallen" in allen.linkedin


def test_parser_survives_a_page_with_no_founders():
    assert parse_yc_founders("<html><body><p>nothing here</p></body></html>") == []


# ------------------------------------------------------- email extraction

def test_extracts_only_on_domain_addresses():
    html = """
    <a href="mailto:stefan@acme.ai">mail</a>
    <p>press@othersite.com and support@acme.ai</p>
    <img src="logo@2x.png">
    """
    assert extract_emails(html, "acme.ai") == {"stefan@acme.ai", "support@acme.ai"}


def test_strips_mailto_query_and_trailing_punctuation():
    html = '<a href="mailto:hello@acme.ai?subject=Hi">x</a> (hello@acme.ai).'
    assert extract_emails(html, "acme.ai") == {"hello@acme.ai"}


def test_enrich_handles_a_company_with_no_yc_url(tmp_path):
    """html is reused for GitHub org detection, so it must always be bound."""
    from src.database import Database
    from src.finder import enrich_company
    from src.models import Company
    from src.scraper import Fetcher
    from src.verifier import Verifier

    db = Database(tmp_path / "t.db")
    cid = db.upsert_company(Company(name="NoYC", domain="noyc.test", website=""))
    row = db.get_company(cid)

    class DeadFetcher(Fetcher):
        def get(self, url, respect_robots=True):
            return None

    result = enrich_company(row, DeadFetcher("t"), Verifier(db=None, ), SETTINGS_FULL)
    assert result.contacts == []
    db.close()


SETTINGS_FULL = SimpleNamespace(**{**SETTINGS.__dict__})


# ------------------------------------------- funding recency and hireability

from src.finder import batch_recency, funding_score, size_fit  # noqa: E402

NOW = 2026


@pytest.mark.parametrize("batch,expected_order", [
    ("Fall 2026", 4), ("Summer 2026", 3), ("Spring 2026", 2), ("Winter 2026", 1),
])
def test_batch_recency_orders_seasons_within_a_year(batch, expected_order):
    assert batch_recency(batch) == pytest.approx(2026 + (expected_order - 1) / 4)


def test_batch_recency_orders_across_years():
    assert batch_recency("Winter 2026") > batch_recency("Fall 2025")
    assert batch_recency("") == 0.0


@pytest.mark.parametrize("team,expected", [
    (25, 2.0), (10, 2.0), (60, 2.0), (7, 1.0), (90, 1.0), (150, 0.0),
    (2, -1.0), (None, 0.0),
])
def test_size_fit(team, expected):
    assert size_fit(team) == expected


def test_recent_money_beats_old_money_at_equal_size():
    fresh = record(batch="Summer 2026", team_size=25)
    stale = record(batch="Winter 2022", team_size=25)
    assert funding_score(fresh, NOW) > funding_score(stale, NOW)


def test_a_two_person_company_does_not_outrank_a_hireable_one():
    """The bug this guards: funding recency must not override hireability."""
    tiny_fresh = record(batch="Fall 2026", team_size=2)
    solid_older = record(batch="Fall 2025", team_size=25)
    assert funding_score(solid_older, NOW) > funding_score(tiny_fresh, NOW)


def test_hiring_flag_outweighs_one_batch_of_recency():
    hiring = record(batch="Winter 2026", team_size=25, isHiring=True)
    quiet = record(batch="Summer 2026", team_size=25, isHiring=False)
    assert funding_score(hiring, NOW) > funding_score(quiet, NOW)


def test_unknown_batch_sinks_to_the_bottom():
    assert funding_score(record(batch=""), NOW) < 0
