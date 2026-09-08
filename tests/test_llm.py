"""The alignment sentence: word cap, claim whitelist, and clean omission.

The claim whitelist is the important one. John asked that emails only assert
things backed by his resume or GitHub, so a sentence naming experience outside
ASPECTS must be rejected rather than sent.
"""

from __future__ import annotations

import pytest

from src.llm import (
    ASPECTS, MAX_RETRIES, Personalizer, WORD_LIMIT, to_ascii, uses_only_allowed,
    word_count,
)

GOOD = ("Reducto's mission to automate document workflows aligns with my work "
        "in computer-vision pipelines and agent-accessible tooling over MCP.")
OFF_LIST = ("Ubicloud's mission to provide an open source cloud aligns with my "
            "work in cloud infrastructure automation and scalable web services.")
TOO_LONG = GOOD.replace("aligns", "and a great many other worthy ambitions besides aligns")


class ScriptedPersonalizer(Personalizer):
    """Personalizer whose HTTP call returns a scripted list of replies."""

    def __init__(self, replies, max_calls=20):
        super().__init__("key", "model", max_calls=max_calls)
        self.replies = list(replies)
        self.asked = 0

    def _ask(self, prompt):
        self.asked += 1
        return self.replies.pop(0) if self.replies else ""


def line(p):
    return p.line_for("Reducto", "agentic document platform", "detail")


# ------------------------------------------------------------ helpers

def test_word_count():
    assert word_count("one two three") == 3
    assert word_count("  ") == 0


def test_ascii_normalisation():
    assert to_ascii("non‑hallucinatory") == "non-hallucinatory"
    assert to_ascii("it’s “fine”…") == "it's \"fine\"..."
    assert to_ascii("a — b") == "a - b"


def test_whitelist_detection():
    assert uses_only_allowed(GOOD)
    assert not uses_only_allowed(OFF_LIST)


def test_aspects_are_unique_and_short_enough_to_fit():
    """Each aspect is copied verbatim into a 24-word sentence, so keep them short."""
    assert len(ASPECTS) == len(set(ASPECTS))
    assert all(word_count(a) <= 6 for a in ASPECTS)


# ------------------------------------------------------- accept / reject

def test_good_sentence_is_used_first_try():
    p = ScriptedPersonalizer([GOOD])
    assert line(p) == GOOD
    assert p.asked == 1


def test_off_list_claim_is_rejected_then_retried():
    p = ScriptedPersonalizer([OFF_LIST, GOOD])
    assert line(p) == GOOD
    assert p.asked == 2


def test_over_long_sentence_is_rejected():
    p = ScriptedPersonalizer([TOO_LONG, GOOD])
    assert word_count(TOO_LONG) > WORD_LIMIT
    assert line(p) == GOOD


def test_empty_response_is_retried():
    """The usual real failure: the reasoning model returns empty content."""
    p = ScriptedPersonalizer(["", "", GOOD])
    assert line(p) == GOOD


def test_paragraph_is_omitted_when_every_attempt_fails():
    """John asked for no filler: an unusable line means no paragraph at all."""
    p = ScriptedPersonalizer([OFF_LIST] * (MAX_RETRIES + 2))
    assert line(p) == ""


def test_omitted_when_no_api_key():
    p = Personalizer("", "model")
    assert p.line_for("X", "y", "z") == ""


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


# ---------------------------------------------- structural shape of the line

from src.llm import well_formed  # noqa: E402

SKILLS_IN_MISSION = ("Synthio Labs's mission to agent-accessible tooling over MCP "
                     "aligns with my work in agent-accessible tooling over MCP.")
DUP_ACROSS_HINGE = ("Reducto's mission to automate documents with agent-accessible "
                    "tooling over MCP aligns with my work in agent-accessible "
                    "tooling over MCP and computer-vision pipelines.")
TRAILING_CLAIM = ("Acme's mission to do useful things aligns with my work in "
                  "computer-vision pipelines and deep distributed systems expertise.")


def test_well_formed_accepts_the_intended_shape():
    assert well_formed(GOOD)


@pytest.mark.parametrize("bad", [
    SKILLS_IN_MISSION,
    DUP_ACROSS_HINGE,
    TRAILING_CLAIM,
    "Acme is great and I like computer-vision pipelines.",
    "Acme's mission aligns with my work in aligns with my work in x.",
    "Short aligns with my work in computer-vision pipelines.",
])
def test_well_formed_rejects_malformed(bad):
    assert not well_formed(bad)


def test_line_for_rejects_gibberish_and_omits(capsys):
    """Gibberish is worse than nothing, so it must not reach an email."""
    p = ScriptedPersonalizer([SKILLS_IN_MISSION] * 6)
    assert line(p) == ""


def test_line_for_recovers_after_a_malformed_attempt():
    p = ScriptedPersonalizer([DUP_ACROSS_HINGE, GOOD])
    assert line(p) == GOOD


# ------------------------------------- relevance: only evident connections

from src.llm import DECLINE, connection_is_evident  # noqa: E402

UBICLOUD = "Ubicloud open source alternative to AWS, cloud servers and hosting"
ARINI = "Arini AI receptionist for dentists that handles patient phone calls"


def test_stretch_connection_is_rejected():
    """A cloud host has nothing to do with computer vision."""
    s = ("Ubicloud's mission to provide open source cloud aligns with my work "
         "in computer-vision pipelines and speech-driven interfaces.")
    assert not connection_is_evident(s, UBICLOUD)


def test_evident_connection_is_kept():
    s = ("Arini's mission to automate dental calls aligns with my work in "
         "speech-driven interfaces and autonomous incident briefings.")
    assert connection_is_evident(s, ARINI)


def test_shipping_fast_alone_never_counts_as_a_connection():
    """"I ship fast" is not a link to any company's mission."""
    s = ("Acme's mission to do something aligns with my work in shipping "
         "full-stack features quickly.")
    assert not connection_is_evident(s, "Acme does something entirely unrelated")


def test_model_declining_omits_without_retrying():
    p = ScriptedPersonalizer([DECLINE, GOOD])
    assert p.line_for("Acme", "unrelated widget maker", "") == ""
    assert p.asked == 1, "a refusal is an answer, not a failure to retry"


def test_irrelevant_but_well_formed_line_is_dropped():
    s = ("Ubicloud's mission to provide open source cloud aligns with my work "
         "in computer-vision pipelines and speech-driven interfaces.")
    p = ScriptedPersonalizer([s] * 6)
    assert p.line_for("Ubicloud", "open source alternative to AWS", "") == ""
