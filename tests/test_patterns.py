"""Address-pattern learning: detection, storage, priors and candidate ordering."""

from __future__ import annotations

import pytest

from src import patterns
from src.database import Database
from src.models import Company


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "p.db")
    yield database
    database.close()


# ------------------------------------------------------------- detection

@pytest.mark.parametrize("email,name,expected", [
    ("stefan@acme.ai", "Stefan Ax", "first"),
    ("stefan.ax@acme.ai", "Stefan Ax", "first.last"),
    ("stefanax@acme.ai", "Stefan Ax", "firstlast"),
    ("stefana@acme.ai", "Stefan Ax", "firstl"),
    ("sax@acme.ai", "Stefan Ax", "flast"),
    ("stefan_ax@acme.ai", "Stefan Ax", "first_last"),
    ("s.ax@acme.ai", "Stefan Ax", "f.last"),
    ("ax@acme.ai", "Stefan Ax", "last"),
    ("hunter2@acme.ai", "Stefan Ax", ""),
])
def test_detect(email, name, expected):
    assert patterns.detect(email, name) == expected


def test_build_and_apply_round_trip():
    for name in patterns.PATTERN_NAMES:
        address = patterns.apply_pattern(name, "Stefan", "Ax", "acme.ai")
        if address:
            assert patterns.detect(address, "Stefan Ax") in patterns.PATTERN_NAMES


def test_build_needs_a_last_name_where_the_pattern_uses_one():
    assert patterns.build("first.last", "Cher", "") == ""
    assert patterns.build("first", "Cher", "") == "cher"


# --------------------------------------------------------------- learning

def test_learning_records_the_domain_convention(db):
    assert patterns.learn(db, "stefan@acme.ai", "Stefan", "Ax") == "first"
    assert patterns.known_pattern(db, "acme.ai") == ("first", 1)


def test_repeated_agreement_builds_evidence(db):
    patterns.learn(db, "stefan@acme.ai", "Stefan", "Ax")
    patterns.learn(db, "dana@acme.ai", "Dana", "Lu")
    assert patterns.known_pattern(db, "acme.ai") == ("first", 2)


def test_a_lone_conflicting_observation_can_correct_a_weak_one(db):
    patterns.learn(db, "stefan@acme.ai", "Stefan", "Ax")
    patterns.learn(db, "d.lu@acme.ai", "Dana", "Lu")
    assert patterns.known_pattern(db, "acme.ai")[0] == "f.last"


def test_well_evidenced_pattern_is_not_overturned_by_one_outlier(db):
    for first, last in [("Stefan", "Ax"), ("Dana", "Lu"), ("Ravi", "Nair")]:
        patterns.learn(db, f"{first.lower()}@acme.ai", first, last)
    patterns.learn(db, "r.nair@acme.ai", "Ravi", "Nair")
    assert patterns.known_pattern(db, "acme.ai") == ("first", 3)


def test_unparseable_address_teaches_nothing(db):
    assert patterns.learn(db, "hunter2@acme.ai", "Stefan", "Ax") == ""
    assert patterns.known_pattern(db, "acme.ai") == ("", 0)


def test_learning_tolerates_a_missing_database():
    assert patterns.learn(None, "a@b.com", "A", "B") == ""
    assert patterns.known_pattern(None, "b.com") == ("", 0)
    assert patterns.global_prior(None) == list(patterns.PATTERN_NAMES)


# ----------------------------------------------------------------- priors

def test_prior_reorders_by_what_has_been_seen(db):
    people = [("Ada", "Lovelace"), ("Grace", "Hopper"), ("Alan", "Turing")]
    for index, (first, last) in enumerate(people):
        patterns.learn(db, f"{first.lower()}.{last.lower()}@d{index}.com", first, last)
    assert patterns.global_prior(db)[0] == "first.last"


def test_prior_is_the_default_order_before_anything_is_learned(db):
    assert patterns.global_prior(db) == list(patterns.PATTERN_NAMES)


def test_prior_never_drops_unseen_patterns(db):
    patterns.learn(db, "stefan@acme.ai", "Stefan", "Ax")
    assert set(patterns.global_prior(db)) == set(patterns.PATTERN_NAMES)


# ------------------------------------------------------ candidate ordering

def test_known_domain_pattern_is_tried_first(db):
    patterns.learn(db, "s.ax@acme.ai", "Stefan", "Ax")
    cands = patterns.ordered_candidates(db, "Dana", "Lu", "acme.ai")
    assert cands[0] == "d.lu@acme.ai", "the domain's own convention outranks the prior"


def test_candidates_are_unique_and_cover_the_patterns(db):
    cands = patterns.ordered_candidates(db, "Stefan", "Ax", "acme.ai")
    assert len(cands) == len(set(cands))
    assert "stefan@acme.ai" in cands and "sax@acme.ai" in cands


def test_candidates_for_a_single_name_do_not_invent_a_surname(db):
    cands = patterns.ordered_candidates(db, "Cher", "", "acme.ai")
    assert cands == ["cher@acme.ai"]
