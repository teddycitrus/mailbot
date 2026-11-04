"""Target-criteria filtering and the YC page parser, with no network access."""

from __future__ import annotations

import json
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


def test_parses_founders_from_the_embedded_page_json():
    # The current YC page: founders only in the data-page attribute.
    page = {"props": {"company": {"founders": [
        {"full_name": "Caleb Peffer", "title": "Founder/CEO", "is_active": True,
         "founder_bio": "CEO at Firecrawl\n",
         "linkedin_url": "https://www.linkedin.com/in/caleb-peffer/"},
        {"full_name": "Gone Person", "title": "Founder", "is_active": False},
    ]}}}
    html = f"<html><body><div data-page='{json.dumps(page)}'></div></body></html>"
    people = parse_yc_founders(html)
    assert [p.full_name for p in people] == ["Caleb Peffer"]
    assert people[0].role == "Founder/CEO"
    assert people[0].bio == "CEO at Firecrawl"
    assert "caleb-peffer" in people[0].linkedin


def test_a_role_title_where_a_name_belongs_is_not_a_founder():
    """A page that puts "Founder/CEO" in the name slot must yield nobody.

    One of these got through, was turned into founderceo@ by the house
    pattern, was greeted as "Hi Founder/CEO" and bounced.
    """
    page = {"props": {"company": {"founders": [
        {"full_name": "Founder/CEO", "title": "", "is_active": True},
        {"full_name": "Co-Founder & CTO", "title": "", "is_active": True},
        {"full_name": "CEO", "title": "", "is_active": True},
    ]}}}
    html = f"<html><body><div data-page='{json.dumps(page)}'></div></body></html>"
    assert parse_yc_founders(html) == []


def test_names_with_digits_or_sign_punctuation_are_not_founders():
    page = {"props": {"company": {"founders": [
        {"full_name": "Agent 47", "title": "", "is_active": True},
        {"full_name": "Jane@Acme", "title": "", "is_active": True},
    ]}}}
    html = f"<html><body><div data-page='{json.dumps(page)}'></div></body></html>"
    assert parse_yc_founders(html) == []


def test_real_names_still_pass_the_name_gate():
    from src.scraper import _looks_like_name
    for name in ("Jane Smith", "Jean-Luc Picard", "Maceo Mark",
                 "Shaun O'Brien", "Jose Garcia", "Ada", "J. K. Rowling"):
        assert _looks_like_name(name), name


def test_robots_is_read_with_our_own_user_agent():
    # Bot firewalls 403 urllib's default agent; the real file allows everyone.
    from src.scraper import Fetcher
    fetcher = Fetcher("mailbot-test")
    seen = []

    def get(url, timeout=None):
        seen.append(fetcher.session.headers["User-Agent"])
        return SimpleNamespace(status_code=200, text="User-agent: *\nDisallow:\n")

    fetcher.session.get = get
    assert fetcher.allowed("https://acme.ai/about")
    assert seen == ["mailbot-test"]


def test_robots_403_still_means_keep_out():
    from src.scraper import Fetcher
    fetcher = Fetcher("mailbot-test")
    fetcher.session.get = lambda url, timeout=None: SimpleNamespace(
        status_code=403, text="")
    assert not fetcher.allowed("https://acme.ai/about")


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


def test_an_address_escaped_inside_a_json_blob_is_decoded_first():
    """A real address found in the wild, and the bounce it was headed for.

    Sites that ship their content in a __NEXT_DATA__ payload escape the
    punctuation around a mailto. Matching the raw source swallowed the escape
    into the local part and stored u003elegal@example.com, which scores like
    any other published address and is addressed to nobody.
    """
    blob = '{"html":"\\\\u003ca href=\\\\u003emailto:legal@acme.ai\\\\u003c/a"}'
    assert extract_emails(blob, "acme.ai") == {"legal@acme.ai"}


def test_html_entities_around_an_address_are_decoded():
    assert extract_emails("write to &lt;sam@acme.ai&gt; today",
                          "acme.ai") == {"sam@acme.ai"}


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


# ------------------------------------------------- several people per company

def _team_company(tmp_path, monkeypatch, people):
    from src import finder
    from src.database import Database
    from src.models import Company
    from src.scraper import Person, SiteScrape

    db = Database(tmp_path / "team.db")
    cid = db.upsert_company(Company(name="Acme", domain="acme.ai",
                                    website="https://acme.ai",
                                    yc_url="https://yc/acme"))
    monkeypatch.setattr(finder, "parse_yc_founders",
                        lambda html: [Person(*p) for p in people])
    monkeypatch.setattr(finder, "scrape_site", lambda *a, **k: SiteScrape())
    return db, db.get_company(cid)


class _FetchAnything:
    def get(self, url, respect_robots=True):
        return "<html></html>"


class _AcceptFirst:
    """Every person's first candidate verifies."""

    def __init__(self, db):
        self.db = db

    def first_verified(self, candidates, domain):
        from src.models import VERIFIED
        from src.verifier import Verdict
        return candidates[0], Verdict(VERIFIED, "250 ok", 90)

    def is_catchall(self, domain):
        return False


def test_enrich_returns_up_to_the_per_company_cap(tmp_path, monkeypatch):
    from src.finder import enrich_company
    people = [("Ada", "Lane", "CEO"), ("Ben", "Ruiz", "CTO"),
              ("Cy", "Moss", "Founder"), ("Di", "Park", "Founder")]
    db, row = _team_company(tmp_path, monkeypatch, people)
    settings = SimpleNamespace(**{**SETTINGS.__dict__, "max_contacts_per_company": 3})
    result = enrich_company(row, _FetchAnything(), _AcceptFirst(db), settings)
    assert len(result.contacts) == 3
    assert len({c.email for c in result.contacts}) == 3
    # Role matches go first, so the CTO and founders beat the CEO.
    assert "Ada" not in {c.first_name for c in result.contacts}
    db.close()


def test_a_cap_of_one_keeps_the_old_behaviour(tmp_path, monkeypatch):
    from src.finder import enrich_company
    db, row = _team_company(tmp_path, monkeypatch,
                            [("Ben", "Ruiz", "CTO"), ("Cy", "Moss", "Founder")])
    settings = SimpleNamespace(**{**SETTINGS.__dict__, "max_contacts_per_company": 1})
    result = enrich_company(row, _FetchAnything(), _AcceptFirst(db), settings)
    assert len(result.contacts) == 1
    db.close()


@pytest.mark.parametrize("email, author, expected", [
    ("justin.aharoni@acme.ai", "Justin Aharoni", ("Justin", "Aharoni")),
    ("justin@acme.ai", "Justin Aharoni", ("Justin", "Aharoni")),
    ("jaharoni@acme.ai", "Justin Aharoni", ("Justin", "Aharoni")),
    ("jacob17@acme.ai", "jacob17", ("", "")),
    ("merlin@acme.ai", "Someone Else", ("", "")),
    ("dev@acme.ai", "", ("", "")),
])
def test_commit_author_names_an_address_only_when_it_fits(email, author, expected):
    from src.finder import _name_from_commit
    assert _name_from_commit(email, author) == expected


# ----------------------------------------------------- provenance on catch-alls

class _CatchAll:
    """A catch-all domain: every probe proves nothing."""

    def __init__(self, db):
        self.db = db

    def verify(self, email, source=None):
        from src.models import CATCHALL
        from src.verifier import Verdict
        return Verdict(CATCHALL, "catch-all domain, address was published", 75)

    def first_verified(self, candidates, domain):
        from src.models import CATCHALL
        from src.verifier import Verdict
        return None, Verdict(CATCHALL, "catch-all domain, guesses unprovable", 25)

    def is_catchall(self, domain):
        return True

    def mx_lookup(self, domain):
        return "mx.acme.ai", True


def test_published_addresses_are_tagged_by_how_they_were_found(tmp_path, monkeypatch):
    from src import finder
    from src.scraper import SiteScrape

    db, company = _team_company(tmp_path, monkeypatch, [("Ann", "Lee", "CEO")])
    monkeypatch.setattr(finder, "scrape_site", lambda *a, **k: SiteScrape(
        emails={"ann@acme.ai", "zed@acme.ai"}))
    settings = SimpleNamespace(target_roles=("ceo",), max_contacts_per_company=3)
    result = finder.enrich_company(company, _FetchAnything(), _CatchAll(db), settings)
    tags = {c.email: c.provenance for c in result.contacts}
    assert tags == {"ann@acme.ai": "founder_published", "zed@acme.ai": "site_unattributed"}
    # The trusted one sorts first, so it gets the per-company cap.
    assert result.contacts[0].email == "ann@acme.ai"


# ----------------------------------------- guesses for the volume lane only

def test_a_catchall_domain_yields_a_reserved_guess_when_asked(tmp_path, monkeypatch):
    from src import finder
    from src.models import CATCHALL, PROV_GUESS, VOLUME_LANE

    db, company = _team_company(tmp_path, monkeypatch, [("Ann", "Lee", "CEO")])
    settings = SimpleNamespace(target_roles=("ceo",), max_contacts_per_company=3,
                               volume_guesses=True)
    result = finder.enrich_company(company, _FetchAnything(), _CatchAll(db), settings)
    assert [(c.email, c.verify_status, c.provenance, c.lane) for c in result.contacts] == [
        ("ann@acme.ai", CATCHALL, PROV_GUESS, VOLUME_LANE)]
    assert result.contacts[0].confidence < 70
    db.close()


def test_without_volume_guesses_a_catchall_domain_stays_parked(tmp_path, monkeypatch):
    from src import finder
    from src.models import SKIP_CATCHALL_UNPROVABLE

    db, company = _team_company(tmp_path, monkeypatch, [("Ann", "Lee", "CEO")])
    settings = SimpleNamespace(target_roles=("ceo",), max_contacts_per_company=3)
    result = finder.enrich_company(company, _FetchAnything(), _CatchAll(db), settings)
    assert result.contacts == [] and result.note == SKIP_CATCHALL_UNPROVABLE
    db.close()
