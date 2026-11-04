"""Opt-out detection against a corpus of synthetic replies.

A missed opt-out is the worst error this system can make: the template
promises "reply and I will not contact you again", so every plausible way of
saying stop has to suppress. The corpus is synthetic; no real recipient text
appears here.

Precision matters in the other direction too: a warm reply wrongly read as an
opt-out is silently suppressed and never answered. The negative cases pin
those down.
"""

from __future__ import annotations

import pytest

from src.inbox import strip_quoted, wants_out


OPT_OUTS = [
    # The promise in the template, kept in its plainest forms.
    "unsubscribe",
    "Unsubscribe me please",
    "please remove me from your list",
    "take me off your list",
    "Please take me off this list.",
    "stop emailing me",
    "please stop contacting me",
    "do not contact me again",
    "don't email me again",
    "Not interested.",
    "no longer interested, thanks",
    "please stop",
    # Phrasings seen in the wild that the first regex missed.
    "opt me out",
    "I want to opt out of these emails",
    "please opt us out",
    "stop sending me these",
    "please stop sending these emails",
    "do not send me any more emails",
    "remove us from your list",
    "take us off your mailing list",
    "please remove this address from your list",
    "don't contact us again",
    "do not reach out again",
    "please don't reach out anymore",
    "leave me alone",
    "cease all communication",
    "no more emails please",
    "Wrong person. Stop emailing.",
    "This is spam. Remove me.",
    "unsub",
    # French, for the Toronto and Montreal targets, and two other languages
    # founders commonly answer in.
    "Merci de ne plus me contacter.",
    "Veuillez me retirer de votre liste",
    "Je ne suis pas intéressé.",
    "Pas intéressée, merci",
    "désabonner",
    "Por favor, no me contacten más",
    "No estoy interesado",
    "Bitte keine E-Mails mehr",
    "Kein Interesse, danke.",
]

NOT_OPT_OUTS = [
    # Warm or neutral replies that must never be suppressed.
    "Thanks for reaching out, let's set up a call next week.",
    "Interesting background! What are you looking for in an internship?",
    "We are interested. Can you share your availability?",
    "I removed the old job posting, but we are still hiring.",
    "We stopped using Django last year, hope that's fine.",
    "Can you send me your resume again?",
    "Our team is remote, does that work for you?",
    "Très intéressant, on se parle jeudi ?",
    "Estamos interesados, hablemos la próxima semana.",
    "Je vais me retirer du projet en janvier, parlez à Marc.",
    # Our own quoted words must not read as the recipient asking out.
    "Sounds good!\n\nOn Mon, Nov 2, 2026 at 9:02 AM John wrote:\n"
    "> reply to this message and I will not contact you again\n"
    "> unsubscribe: mailto:john@example.test",
    "Let's talk.\n\nFrom: John Mannully\nSent: Monday\n"
    "To: jane@acme.test\nSubject: opportunities\n"
    "reply and I will not contact you again. unsubscribe here.",
]


@pytest.mark.parametrize("body", OPT_OUTS)
def test_every_way_of_saying_stop_is_honoured(body):
    assert wants_out(body), f"missed opt-out: {body!r}"


@pytest.mark.parametrize("body", NOT_OPT_OUTS)
def test_interest_is_never_read_as_an_opt_out(body):
    assert not wants_out(body), f"false opt-out: {body!r}"


def test_quoted_template_text_is_stripped_before_matching():
    body = ("See you Thursday.\n\nOn Tue wrote:\n"
            "> if you would rather not hear from me, reply unsubscribe")
    assert strip_quoted(body).strip() == "See you Thursday."
    assert not wants_out(body)
