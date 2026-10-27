"""The personal line: John's half is fixed text, the model only describes them.

The claim rule is the important one. John asked that emails only assert things
backed by his resume or GitHub, so nothing the model writes may speak for him,
and a company with no plain overlap gets no line at all.
"""

from __future__ import annotations

import pytest

from src.llm import (
    BANNED, FACT_LINES, MAX_RETRIES, OPENERS, Personalizer, WORD_LIMIT,
    clean_what, compose, match_aspect, to_ascii, word_count,
)

AGENTS = "The infrastructure to run coding agents for your team"
GOOD = "giving coding agents a place to run for whole teams"


class ScriptedPersonalizer(Personalizer):
    """Personalizer whose HTTP call returns a scripted list of replies."""

    def __init__(self, replies, max_calls=20):
        super().__init__("key", "model", max_calls=max_calls)
        self.replies = list(replies)
        self.asked = 0

    def _ask(self, prompt):
        self.asked += 1
        return self.replies.pop(0) if self.replies else ""


def line(p, one_liner=AGENTS, description=""):
    return p.line_for("Agent Relay", one_liner, description)


# ------------------------------------------------------------ helpers

def test_word_count():
    assert word_count("one two three") == 3
    assert word_count("  ") == 0


def test_ascii_normalisation():
    assert to_ascii("non‑hallucinatory") == "non-hallucinatory"
    assert to_ascii("it’s “fine”…") == "it's \"fine\"..."
    assert to_ascii("a — b") == "a - b"


def test_fact_lines_are_plain_and_short():
    """John's half is sent verbatim, so it must obey his own style rules."""
    for lines in FACT_LINES.values():
        for fact in lines:
            assert ";" not in fact and "—" not in fact and "–" not in fact
            assert fact.isascii()
            assert not any(stem in fact.lower() for stem in BANNED)
            assert word_count(fact) <= 32


def test_every_composed_line_fits_the_word_limit():
    longest = "w" + " word" * 11
    for opener in OPENERS:
        for aspect, lines in FACT_LINES.items():
            for fact in lines:
                assert word_count(f"{opener} {longest}. {fact}") <= WORD_LIMIT


# ------------------------------------------------------------ matching

@pytest.mark.parametrize("one_liner, aspect", [
    (AGENTS, "agent-accessible tooling over MCP"),
    ("Turn any website into an MCP for your agents", "agent-accessible tooling over MCP"),
    ("The AI fraud hunter", "real-time risk monitoring"),
    ("Speaking coach for non-native English professionals", "speech-driven interfaces"),
    ("100% automated inspection for consumer electronics parts", "computer-vision pipelines"),
    ("Robots for vehicle maintenance.", "computer-vision pipelines"),
])
def test_plain_overlaps_are_matched(one_liner, aspect):
    assert match_aspect(one_liner) == aspect


@pytest.mark.parametrize("one_liner", [
    "Open source alternative to AWS",
    "Build games at the speed of thought",
    "AI Video Production for Professionals",          # generating, not seeing
    "Bookkeeping for SMBs, contractors & ecommerce",  # ecommerce as a customer
    "Maps what employees do and what it costs",
])
def test_stretches_get_no_match(one_liner):
    assert match_aspect(one_liner) == ""


def test_a_passing_mention_in_the_description_is_not_enough():
    assert match_aspect("Build games fast", "players can use voice chat") == ""


def test_shipping_fast_never_matches_on_its_own():
    assert "shipping full-stack features quickly" not in FACT_LINES


# ------------------------------------------------------------ the phrase

@pytest.mark.parametrize("raw, expected", [
    (GOOD, GOOD),
    (f"WHAT: {GOOD}.", GOOD),
    (f"Saw you're {GOOD}", GOOD),
])
def test_clean_what_accepts_and_tidies(raw, expected):
    assert clean_what(raw) == expected


@pytest.mark.parametrize("bad", [
    "",
    "agents for teams",                                       # no -ing verb
    "building",                                               # too short
    "building " + "very " * 12 + "long things",               # too long
    "building agents; and more for teams",                    # semicolon
    "building agents — fast, for teams",                      # em dash
    "building agents for teams!",                             # exclamation
    "revolutionizing how teams run their coding agents",      # marketing word
    "building a seamless home for coding agents",
    "building the leading platform for coding agents",
])
def test_clean_what_rejects_generated_sounding_phrases(bad):
    assert clean_what(bad) == ""


def test_compose_restores_names_and_ai():
    out = compose("Mage", "building mage's ai tools for app developers",
                  "agent-accessible tooling over MCP")
    assert "Mage's AI tools" in out
    assert "an API for" in compose("X", "turning any website into an api for agents",
                                   "agent-accessible tooling over MCP")
    assert "maintenance" in compose("X", "building ai tools for maintenance crews",
                                    "computer-vision pipelines")


def test_compose_is_stable_per_company():
    a = compose("Acme", GOOD, "agent-accessible tooling over MCP")
    assert a == compose("Acme", GOOD, "agent-accessible tooling over MCP")


# ------------------------------------------------------- line_for

def test_good_phrase_is_used_first_try():
    p = ScriptedPersonalizer([GOOD])
    out = line(p)
    assert GOOD in out and "Nora" in out
    assert p.asked == 1


def test_no_overlap_means_no_line_and_no_model_call():
    p = ScriptedPersonalizer([GOOD])
    assert line(p, one_liner="Open source alternative to AWS") == ""
    assert p.asked == 0


def test_bad_phrase_is_retried():
    p = ScriptedPersonalizer(["revolutionizing agent infrastructure", GOOD])
    assert GOOD in line(p)
    assert p.asked == 2


def test_empty_response_is_retried():
    """The usual real failure: the reasoning model returns empty content."""
    p = ScriptedPersonalizer(["", "", GOOD])
    assert GOOD in line(p)


def test_paragraph_is_omitted_when_every_attempt_fails():
    p = ScriptedPersonalizer(["nope"] * (MAX_RETRIES + 2))
    assert line(p) == ""


def test_omitted_when_no_api_key():
    p = Personalizer("", "model")
    assert line(p) == ""


def test_omitted_when_budget_spent():
    p = ScriptedPersonalizer([GOOD], max_calls=0)
    assert line(p) == ""
    assert p.asked == 0


def test_retries_are_bounded():
    p = ScriptedPersonalizer([""] * 50)
    assert line(p) == ""
    assert p.asked <= MAX_RETRIES + 1


def test_budget_caps_retries_before_max_retries():
    p = ScriptedPersonalizer([""] * 50, max_calls=2)
    assert line(p) == ""
    assert p.asked == 2
