"""GitHub org discovery: filtering and fan-out, with no network access."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from src.gh_discovery import (
    aliases_for, build_query, discover, domain_of, ordered_queries, to_company,
    usable_domain,
)

SETTINGS = SimpleNamespace(
    target_locations=("san francisco", "new york", "toronto"),
    max_company_age_years=5,
)


# ------------------------------------------------------------ website parsing

@pytest.mark.parametrize("blog,expected", [
    ("https://www.acme.ai/", "acme.ai"),
    ("acme.ai", "acme.ai"),
    ("HTTP://Acme.AI/careers", "acme.ai"),
    ("https://acme.ai:8080", "acme.ai"),
    ("", ""),
    ("not a url", ""),
])
def test_domain_of_normalises_hand_typed_profile_fields(blog, expected):
    assert domain_of(blog) == expected


@pytest.mark.parametrize("domain", [
    "acme.github.io", "github.io", "acme.vercel.app", "medium.com",
    "linktr.ee", "stanford.edu", "nasa.gov", "",
    # Academic orgs are the main false positive a city search surfaces.
    "civmin.utoronto.ca", "utoronto.ca", "cs.ox.ac.uk", "anu.edu.au",
    "hmrc.gov.uk",
])
def test_usable_domain_rejects_hosts_we_cannot_mail(domain):
    assert not usable_domain(domain)


@pytest.mark.parametrize("domain", ["acme.ai", "evidence.dev", "1password.com"])
def test_usable_domain_keeps_real_company_domains(domain):
    assert usable_domain(domain)


# ------------------------------------------------------------------- querying

def test_build_query_derives_the_cutoff_from_the_age_setting():
    q = build_query("Toronto", 5, today=date(2026, 9, 9))
    assert 'location:"Toronto"' in q
    assert "created:>2021-09-09" in q
    assert "type:org" in q


def test_aliases_cover_the_spellings_people_actually_use():
    assert "NYC" in aliases_for("new york")
    assert "Bay Area" in aliases_for("San Francisco")
    # An unconfigured location still searches for itself rather than vanishing.
    assert aliases_for("austin") == ("Austin",)


# -------------------------------------------------------- profile to Company

def test_to_company_carries_the_org_slug_and_founding_proxy():
    company = to_company({
        "login": "reworkd", "name": "Reworkd", "blog": "https://reworkd.ai",
        "created_at": "2022-12-09T00:00:00Z", "bio": "Agents that scrape.",
    }, "San Francisco")
    assert company.domain == "reworkd.ai"
    assert company.github_org == "reworkd"
    assert company.founded_year == 2022
    assert company.source == "github"


def test_to_company_rejects_an_org_with_no_website():
    assert to_company({"login": "acode", "blog": ""}, "Toronto") is None


def test_to_company_falls_back_to_the_login_when_unnamed():
    company = to_company({"login": "acme", "name": "", "blog": "acme.ai"}, "Toronto")
    assert company.name == "acme"


# ---------------------------------------------------------------- the fan-out

class FakeClient:
    """Returns three orgs per city on page 1 and nothing after."""

    exhausted = False

    def __init__(self, pages: dict | None = None):
        self.pages = pages if pages is not None else {}
        self.searches: list[str] = []

    def search_orgs(self, query, per_page=100, page=1):
        self.searches.append(query)
        return self.pages.get((query, page), [])

    def get_user(self, login):
        return {"login": login, "name": login.title(),
                "blog": f"https://{login}.com", "created_at": "2023-01-01T00:00:00Z"}


class FakeDB:
    def __init__(self, known=()):
        self.known = set(known)

    def company_exists(self, domain):
        return domain in self.known


def _pages_for(city_logins: dict[str, list[str]]):
    pages = {}
    for alias, logins in city_logins.items():
        query = build_query(alias, 5, today=date(2026, 9, 9))
        pages[(query, 1)] = [{"login": name} for name in logins]
    return pages


def test_discover_interleaves_cities_so_toronto_is_not_starved():
    """SF has the most orgs; draining it in order would spend the whole limit."""
    client = FakeClient(_pages_for({
        "San Francisco": ["sf1", "sf2", "sf3"],
        "New York": ["ny1", "ny2"],
        "Toronto": ["to1"],
    }))
    found, stats = discover(client, SETTINGS, FakeDB(), limit=3,
                            max_pages=1, today=date(2026, 9, 9))
    assert [c.github_org for c in found] == ["sf1", "ny1", "to1"]
    assert stats.added == 3


def test_discover_skips_domains_already_in_the_database():
    client = FakeClient(_pages_for({"Toronto": ["to1", "to2"]}))
    found, stats = discover(client, SETTINGS, FakeDB(known={"to1.com"}),
                            limit=5, max_pages=1, today=date(2026, 9, 9))
    assert [c.domain for c in found] == ["to2.com"]
    assert stats.already_known == 1


def test_discover_stops_when_the_api_budget_is_gone():
    client = FakeClient(_pages_for({"San Francisco": ["sf1", "sf2"]}))
    client.exhausted = True
    found, _ = discover(client, SETTINGS, FakeDB(), limit=5,
                        max_pages=1, today=date(2026, 9, 9))
    assert found == []


def test_query_order_asks_every_city_before_any_second_spelling():
    """A small run limit must not be spent on three spellings of one city."""
    order = ordered_queries(("san francisco", "new york", "toronto"))
    assert [alias for _, alias in order[:3]] == ["San Francisco", "New York", "Toronto"]
    # Toronto has fewer spellings than the others; the tail must not misalign.
    assert [city for city, _ in order] == [
        "San Francisco", "New York", "Toronto",
        "San Francisco", "New York", "Toronto",
        "San Francisco", "New York",
    ]
