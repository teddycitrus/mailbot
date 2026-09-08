"""Reply and opt-out handling.

The template promises "reply and I will not contact you again", so the opt-out
path is a commitment, not a nicety. The trap it has to avoid: our own outgoing
text contains the word "unsubscribe", so a reply quoting the original must not
be read as an opt-out request.
"""

from __future__ import annotations

import pytest

from src.inbox import is_bounce, strip_quoted, wants_out

OUR_EMAIL_QUOTED = """Sure, let's talk. Are you free Thursday?

On Tue, 8 Sep 2026 at 08:35, Ada Lovelace <sender@example.com> wrote:
> Dear Stefan,
> I'm John!
> Not interested? Reply to this message or write to sender@example.com
> and I will not contact you again.
"""

REAL_OPT_OUT = """Please remove me from your list.

On Tue, 8 Sep 2026 at 08:35, Ada Lovelace <sender@example.com> wrote:
> Dear Stefan,
> Not interested? Reply to this message.
"""


# ------------------------------------------------------------ quote stripping

def test_quoted_original_is_removed():
    kept = strip_quoted(OUR_EMAIL_QUOTED)
    assert "Are you free Thursday" in kept
    assert "not contact you again" not in kept


@pytest.mark.parametrize("marker", [
    "On Mon, 1 Jan 2026 at 09:00, Someone <a@b.com> wrote:",
    "-----Original Message-----",
    "From: Someone <a@b.com>",
])
def test_common_quote_markers_cut_the_body(marker):
    text = f"my actual words\n\n{marker}\nquoted stuff about unsubscribe\n"
    assert "quoted stuff" not in strip_quoted(text)
    assert "my actual words" in strip_quoted(text)


def test_angle_quoted_lines_are_dropped():
    assert ">" not in strip_quoted("hello\n> quoted unsubscribe line\n")


# --------------------------------------------------------------- opt-out

def test_positive_reply_quoting_our_footer_is_not_an_opt_out():
    """The regression that matters: our own footer must not opt someone out."""
    assert not wants_out(OUR_EMAIL_QUOTED)


def test_genuine_opt_out_is_detected():
    assert wants_out(REAL_OPT_OUT)


@pytest.mark.parametrize("body", [
    "please unsubscribe me",
    "Remove me from this list",
    "take me off your list please",
    "stop emailing me",
    "Not interested, thanks",
    "do not contact me again",
    "Please stop.",
])
def test_opt_out_phrasings(body):
    assert wants_out(body)


@pytest.mark.parametrize("body", [
    "Thanks for reaching out, let's chat next week.",
    "Interesting projects. What's your availability?",
    "Forwarding this to our hiring lead.",
    "We're not hiring interns right now but keep in touch.",
])
def test_ordinary_replies_are_not_opt_outs(body):
    assert not wants_out(body)


# --------------------------------------------------------------- bounces

@pytest.mark.parametrize("sender,subject", [
    ("mailer-daemon@googlemail.com", "Delivery Status Notification (Failure)"),
    ("postmaster@acme.ai", "Undeliverable: your message"),
    ("MAILER-DAEMON@x.com", "returned mail"),
])
def test_bounce_senders_are_recognised(sender, subject):
    assert is_bounce(sender, subject)


def test_a_normal_reply_is_not_a_bounce():
    assert not is_bounce("stefan@acme.ai", "Re: opportunities @ Acme")
