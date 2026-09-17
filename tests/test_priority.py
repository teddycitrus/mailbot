"""The hand-written priority list.

Two things are being protected here. One is that a named company actually
jumps the size and age gates, because most of the names worth a tip fail them.
The other is that a guessed domain is never trusted on the strength of the
guess alone: the site has to say the company's name back, or the entry is left
for a human. Mailing a stranger who happens to own the .com is the failure
this file exists to prevent.
"""

from __future__ import annotations

from types import SimpleNamespace

from src import priority
from src.finder import qualifies


def settings(**over):
    base = dict(target_locations=("san francisco",), max_employees=200,
                max_company_age_years=5, hiring_only=False)
    base.update(over)
    return SimpleNamespace(**base)


def record(**over):
    base = dict(name="Acme", status="Active", website="https://acme.ai",
                all_locations="San Francisco, CA, USA", team_size=20,
                batch="W2025", isHiring=True)
    base.update(over)
    return base


# ---------------------------------------------------------------- parsing

def test_parse_reads_name_and_domain():
    entries = priority.parse("Ramp = ramp.com\nCognition\n")
    assert [(e.name, e.domain) for e in entries] == [
        ("Ramp", "ramp.com"), ("Cognition", ""),
    ]


def test_parse_ignores_comments_and_blanks():
    entries = priority.parse("# a note\n\nRamp = ramp.com   # trailing\n")
    assert len(entries) == 1 and entries[0].domain == "ramp.com"


def test_parse_drops_duplicate_names():
    entries = priority.parse("Ramp = ramp.com\nramp\nRAMP = other.com\n")
    assert len(entries) == 1


def test_render_round_trips_through_parse():
    entries = priority.parse("Ramp = ramp.com\nCognition\n")
    assert [(e.name, e.domain) for e in priority.parse(
        priority.render(entries))] == [("Ramp", "ramp.com"), ("Cognition", "")]


def test_render_keeps_the_reason_a_name_was_not_resolved():
    entries = [priority.Entry("Normal", "", "no candidate domain takes mail")]
    assert "# no candidate domain takes mail" in priority.render(entries)


# --------------------------------------------------------------- guessing

def test_stems_tries_the_name_without_its_suffix():
    assert priority.stems("Pangram Labs") == ["pangramlabs", "pangram-labs",
                                              "pangram"]


def test_candidates_are_ordered_com_first():
    assert priority.candidates("Cognition")[0] == "cognition.com"
    assert "cognition.ai" in priority.candidates("Cognition")


def test_site_confirms_needs_the_name_on_the_page():
    assert priority.site_confirms("<title>Cognition Labs</title>", "Cognition")
    assert not priority.site_confirms("<title>Domain for sale</title>",
                                      "Cognition")


def test_site_confirms_ignores_punctuation_and_case():
    assert priority.site_confirms("<h1>BRAIN CO.</h1>", "Brain Co.")


def test_short_names_are_never_confirmed_by_a_page():
    # "Re" would match almost any page that contains the word "re".
    assert not priority.site_confirms("<p>we are Re, a company</p>", "Re")


def test_resolve_refuses_a_short_name_without_touching_the_network():
    def explode(*_a, **_k):
        raise AssertionError("should not have been called")

    domain, note = priority.resolve(
        "Re", SimpleNamespace(get=explode), SimpleNamespace(mx_host=explode))
    assert domain == "" and "by hand" in note


def test_resolve_accepts_a_domain_whose_site_names_the_company():
    verifier = SimpleNamespace(mx_host=lambda d: "mx.example" if d == "cognition.ai" else "")
    fetcher = SimpleNamespace(get=lambda url: "<title>Cognition</title>")
    assert priority.resolve("Cognition", fetcher, verifier)[0] == "cognition.ai"


def test_resolve_rejects_a_domain_that_takes_mail_but_is_someone_else():
    verifier = SimpleNamespace(mx_host=lambda d: "mx.example")
    fetcher = SimpleNamespace(get=lambda url: "<title>Buy this domain</title>")
    domain, note = priority.resolve("Quadrillion", fetcher, verifier)
    assert domain == "" and "none names the company" in note


def test_resolve_says_so_when_nothing_takes_mail():
    verifier = SimpleNamespace(mx_host=lambda d: "")
    fetcher = SimpleNamespace(get=lambda url: "")
    domain, note = priority.resolve("Quadrillion", fetcher, verifier)
    assert domain == "" and note == "no candidate domain takes mail"


# ------------------------------------------------------------- membership

def test_priority_set_matches_on_name_or_domain():
    known = priority.PrioritySet(priority.parse("Brain Co. = braincompany.com\n"))
    assert known.has(name="brain co")
    assert known.has(name="BRAIN CO.")
    assert known.has(domain="braincompany.com")
    assert not known.has(name="Brains")


def test_empty_priority_set_is_falsy():
    assert not priority.PrioritySet([])


# ------------------------------------------------------------- the bypass

def test_a_listed_company_skips_the_size_gate():
    known = priority.PrioritySet(priority.parse("Acme\n"))
    assert qualifies(record(team_size=4000), settings(), 2026, known)[0]


def test_a_listed_company_skips_the_age_gate():
    known = priority.PrioritySet(priority.parse("Acme\n"))
    assert qualifies(record(batch="W2011"), settings(), 2026, known)[0]


def test_a_listed_company_skips_the_location_gate():
    known = priority.PrioritySet(priority.parse("Acme\n"))
    assert qualifies(record(all_locations="Berlin, Germany"),
                     settings(), 2026, known)[0]


def test_a_listed_company_matched_by_domain_skips_the_gates():
    known = priority.PrioritySet(priority.parse("Something Else = acme.ai\n"))
    assert qualifies(record(team_size=4000), settings(), 2026, known)[0]


def test_an_unlisted_company_still_faces_the_gates():
    known = priority.PrioritySet(priority.parse("Other\n"))
    ok, why = qualifies(record(team_size=4000), settings(), 2026, known)
    assert not ok and "team_size" in why


def test_priority_never_revives_a_dead_company():
    known = priority.PrioritySet(priority.parse("Acme\n"))
    ok, why = qualifies(record(status="Inactive"), settings(), 2026, known)
    assert not ok and why == "inactive"


def test_priority_never_rescues_a_company_with_no_website():
    known = priority.PrioritySet(priority.parse("Acme\n"))
    ok, why = qualifies(record(website=""), settings(), 2026, known)
    assert not ok and why == "no website"


def test_gates_are_unchanged_when_there_is_no_list():
    assert qualifies(record(), settings(), 2026, None)[0]
    assert not qualifies(record(team_size=4000), settings(), 2026, None)[0]
