"""Template rules, the send window, message shape and bounce parsing."""

from __future__ import annotations

from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import pytest

from src.emailer import (
    Template, build_message, extract_bounced_addresses, in_send_window,
    is_hard_bounce, load_template, pace_delay, unedited_markers,
    validate_template,
)

TZ = "America/Toronto"
START, END = dtime(8, 30), dtime(10, 0)


def _at(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(TZ))


# ------------------------------------------------------------- templates

def test_load_requires_a_subject_line(tmp_path):
    path = tmp_path / "t.txt"
    path.write_text("Hi there\n\nno subject header", encoding="utf-8")
    with pytest.raises(ValueError, match="Subject:"):
        load_template(path)


def test_load_requires_a_body(tmp_path):
    path = tmp_path / "t.txt"
    path.write_text("Subject: hello\n\n   \n", encoding="utf-8")
    with pytest.raises(ValueError, match="no body"):
        load_template(path)


def test_load_splits_subject_and_body(tmp_path):
    path = tmp_path / "t.txt"
    path.write_text("Subject: Hi {company}\n\nBody for {first_name}\n", encoding="utf-8")
    template = load_template(path)
    assert template.subject == "Hi {company}"
    assert "{first_name}" in template.body
    assert template.placeholders == {"company", "first_name"}


def test_render_fills_known_fields():
    template = Template("Hi {company}", "Hello {first_name}, from {sender_name}\n")
    subject, body = template.render(
        {"company": "Acme", "first_name": "Stefan", "sender_name": "Jane"}
    )
    assert subject == "Hi Acme"
    assert "Hello Stefan, from Jane" in body


def test_render_does_not_raise_on_a_missing_field():
    template = Template("Hi", "Hello {nobody}\n")
    _, body = template.render({})
    assert "Hello" in body


def test_validate_rejects_unknown_placeholders():
    template = Template("Hi", "I work on {my_secret_field}\n")
    with pytest.raises(ValueError, match="cannot fill"):
        validate_template(template)


def test_unedited_markers_are_detected_not_raised():
    """Markers no longer block queuing; send() is where they are refused."""
    template = Template("Hi {company}", "My background is in [YOUR FOCUS AREA]\n")
    validate_template(template)
    assert unedited_markers(template.body) == ["[YOUR FOCUS AREA]"]


def test_unedited_markers_finds_several_and_dedupes():
    text = "[ONE THING] then [TWO THINGS] then [ONE THING]"
    assert unedited_markers(text) == ["[ONE THING]", "[TWO THINGS]"]


@pytest.mark.parametrize("text", [
    "a normal sentence with [x] lowercase brackets",
    "an array index like arr[0] should not count",
    "no brackets at all",
    "[AB] is too short to be a marker",
])
def test_unedited_markers_ignores_ordinary_brackets(text):
    assert unedited_markers(text) == []


def test_validate_accepts_a_finished_template():
    template = Template("Hi {company}", "Hello {first_name}, {personal_note}\n")
    validate_template(template)


# ----------------------------------------------------------- send window

def test_inside_window_on_a_weekday():
    ok, why = in_send_window(TZ, START, END, _at(2026, 9, 8, 9, 15))
    assert ok and why == ""


@pytest.mark.parametrize("hh,mm", [(8, 29), (10, 1), (3, 0), (17, 0)])
def test_outside_window_is_refused(hh, mm):
    ok, why = in_send_window(TZ, START, END, _at(2026, 9, 8, hh, mm))
    assert not ok and "outside window" in why


def test_boundaries_are_inclusive():
    assert in_send_window(TZ, START, END, _at(2026, 9, 8, 8, 30))[0]
    assert in_send_window(TZ, START, END, _at(2026, 9, 8, 10, 0))[0]


@pytest.mark.parametrize("day", [5, 6])
def test_weekends_are_refused(day):
    ok, why = in_send_window(TZ, START, END, _at(2026, 9, 5 + (day - 5), 9, 0))
    assert not ok and "weekend" in why


def test_pace_delay_is_jittered_but_bounded():
    values = {pace_delay(20) for _ in range(50)}
    assert len(values) > 1
    assert all(12 <= v <= 32 for v in values)


# ---------------------------------------------------------- message shape

def test_message_carries_unsubscribe_and_identity():
    msg = build_message(
        "Hi Acme", "Body\n", "stefan@acme.ai", "Stefan Ax",
        "jane@example.com", "Jane Doe", "jane@example.com", "jane@example.com",
    )
    assert msg["To"] == "Stefan Ax <stefan@acme.ai>"
    assert msg["From"] == "Jane Doe <jane@example.com>"
    assert "mailto:jane@example.com" in msg["List-Unsubscribe"]
    assert msg["Message-ID"].endswith("example.com>")
    assert msg.get_body(preferencelist=("plain",)).get_content().strip() == "Body"


def test_attachment_is_embedded(tmp_path):
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    msg = build_message("s", "b\n", "a@b.com", "", "j@x.com", "J",
                        attachments=[str(pdf)])
    names = [p.get_filename() for p in msg.iter_attachments()]
    assert names == ["resume.pdf"]


def test_missing_attachment_fails_loudly(tmp_path):
    with pytest.raises(FileNotFoundError):
        build_message("s", "b\n", "a@b.com", "", "j@x.com", "J",
                      attachments=[str(tmp_path / "nope.pdf")])


# --------------------------------------------------------------- bounces

DSN = """From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>
Subject: Delivery Status Notification (Failure)
Content-Type: multipart/report; report-type=delivery-status

Action: failed
Final-Recipient: rfc822; stefan@acme.ai
Diagnostic-Code: smtp; 550 5.1.1 The email account does not exist.
"""


def test_extracts_the_failed_recipient():
    assert extract_bounced_addresses(DSN) == {"stefan@acme.ai"}


def test_own_address_is_not_treated_as_bounced():
    raw = DSN + "\nReporting-MTA: dns; mail.example.com\njane@example.com\n"
    assert "jane@example.com" not in extract_bounced_addresses(raw, "example.com")


def test_hard_bounce_is_detected():
    assert is_hard_bounce(DSN)
    assert not is_hard_bounce("Subject: out of office\n\n4.2.2 mailbox full, retrying")


def test_prose_bounce_without_dsn_headers():
    raw = "Your message to nobody@acme.ai was rejected: user unknown"
    assert "nobody@acme.ai" in extract_bounced_addresses(raw)


# ------------------------------------------------------ markdown rendering

from src.emailer import md_to_html, md_to_plain  # noqa: E402


def test_plain_flattens_links_readably():
    assert md_to_plain("[Eureka](https://gh.com/x)") == "Eureka (https://gh.com/x)"


def test_plain_collapses_a_bare_url_label():
    """A link whose label is the URL should not print the URL twice."""
    url = "https://github.com/example-user"
    assert md_to_plain(f"[{url}]({url})") == url


def test_html_makes_links_clickable():
    html = md_to_html("[Nora](https://gh.com/n)")
    assert '<a href="https://gh.com/n">Nora</a>' in html


def test_html_wraps_bullets_in_a_list():
    html = md_to_html("- one\n- two\n\nafter")
    assert html.count("<li>") == 2
    assert "<ul>" in html and "</ul>" in html
    assert "after" in html


def test_html_escapes_markup_in_the_subject_line_text():
    html = md_to_html("<30 seconds & counting")
    assert "&lt;30 seconds &amp; counting" in html


def test_message_carries_both_plain_and_html_parts():
    body = "Dear X,\n\n- built [Eureka](https://gh.com/x)\n\nBest,\nJohn\n"
    msg = build_message("s", body, "a@b.com", "A", "j@x.com", "J")
    plain = msg.get_body(preferencelist=("plain",)).get_content()
    html = msg.get_body(preferencelist=("html",)).get_content()
    assert "Eureka (https://gh.com/x)" in plain
    assert '<a href="https://gh.com/x">Eureka</a>' in html


def test_attachment_survives_the_html_alternative(tmp_path):
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    body = "Hi\n\n[View my resume](https://drive.google.com/x)\n"
    msg = build_message("s", body, "a@b.com", "", "j@x.com", "J",
                        attachments=[str(pdf)])
    assert [p.get_filename() for p in msg.iter_attachments()] == ["resume.pdf"]
    assert msg.get_body(preferencelist=("html",)) is not None


# ------------------------------------------------ recipient-local send window

from src.emailer import local_window_open, recipient_timezone  # noqa: E402


@pytest.mark.parametrize("location,expected", [
    ("San Francisco, CA, USA", "America/Los_Angeles"),
    ("New York City, NY, USA", "America/New_York"),
    ("Toronto, ON, Canada", "America/Toronto"),
    ("Brooklyn, NY", "America/New_York"),
    ("Palo Alto, CA", "America/Los_Angeles"),
])
def test_location_maps_to_timezone(location, expected):
    assert recipient_timezone(location, "America/Toronto") == expected


def test_unknown_location_falls_back_to_sender_zone():
    assert recipient_timezone("Reykjavik, Iceland", "America/Toronto") == "America/Toronto"
    assert recipient_timezone("", "America/Toronto") == "America/Toronto"


def _utc(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo("UTC"))


def test_east_coast_is_open_while_west_coast_waits():
    """12:35 UTC on a Tuesday is 08:35 in New York but only 05:35 in SF."""
    when = _utc(2026, 9, 8, 12, 35)
    east, _ = local_window_open("New York City, NY", TZ, START, END, when)
    west, why = local_window_open("San Francisco, CA", TZ, START, END, when)
    assert east is True
    assert west is False and "05:35" in why


def test_west_coast_opens_three_hours_later():
    when = _utc(2026, 9, 8, 15, 35)  # 08:35 Pacific
    west, zone = local_window_open("San Francisco, CA", TZ, START, END, when)
    east, _ = local_window_open("New York City, NY", TZ, START, END, when)
    assert west is True and zone == "America/Los_Angeles"
    assert east is False, "New York is past its window by then"


def test_recipient_window_still_refuses_weekends():
    when = _utc(2026, 9, 12, 15, 35)  # Saturday
    ok, why = local_window_open("San Francisco, CA", TZ, START, END, when)
    assert not ok and "weekend" in why
