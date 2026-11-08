"""Reply and opt-out handling.

The template promises "reply and I will not contact you again", so the opt-out
path is a commitment, not a nicety. The trap it has to avoid: our own outgoing
text contains the word "unsubscribe", so a reply quoting the original must not
be read as an opt-out request.
"""

from __future__ import annotations

import email as email_lib
from types import SimpleNamespace

import pytest

from src import inbox
from src.database import Database
from src.inbox import (
    build_reply_draft, drafts_mailbox, is_auto_reply, is_bounce,
    load_reply_template, reply_first_name, scan_inbox, strip_quoted, wants_out,
)
from src.models import Company, Contact

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


# ------------------------------------------------------------ reply drafts

REPLY_TEMPLATE = """Subject: Re: {original_subject}

Dear {first_name},

The work you guys do at {company} is interesting.

Best regards,
Ada
[https://x.com/someone](https://x.com/someone)
"""


def _reply(sender="Stefan Weber <stefan@acme.ai>", subject="Re: opportunities @ Acme",
           body="Not hiring right now, sorry.", extra=""):
    return email_lib.message_from_string(
        f"From: {sender}\nTo: Ada <ada@example.com>\nSubject: {subject}\n"
        f"Message-ID: <reply-1@acme.ai>\nReferences: <orig-1@example.com>\n"
        f"In-Reply-To: <orig-1@example.com>\n{extra}\n{body}\n"
    )


def _settings(tmp_path, template=REPLY_TEMPLATE):
    path = tmp_path / "reply.txt"
    if template is not None:
        path.write_text(template, encoding="utf-8")
    return SimpleNamespace(
        from_email="ada@example.com", from_name="Ada Lovelace", reply_to="",
        imap_host="imap.example.com", imap_port=993, imap_user="u", imap_pass="p",
        reply_template_path=path,
    )


@pytest.mark.parametrize("extra,subject", [
    ("Auto-Submitted: auto-replied", "Re: opportunities @ Acme"),
    ("X-Autoreply: yes", "Re: opportunities @ Acme"),
    ("Precedence: auto_reply", "Re: opportunities @ Acme"),
    ("", "Automatic reply: opportunities @ Acme"),
    ("", "Out of Office: back Monday"),
])
def test_machine_answers_are_auto_replies(extra, subject):
    assert is_auto_reply(_reply(subject=subject, extra=extra))


def test_a_person_is_not_an_auto_reply():
    # Our own outgoing header is "Auto-Submitted: no", which is not automatic.
    assert not is_auto_reply(_reply(extra="Auto-Submitted: no"))


def test_greeting_prefers_stored_name_then_display_name_then_address():
    assert reply_first_name("Stefan", "Bob Smith <bob@acme.ai>") == "Stefan"
    assert reply_first_name("", '"grace hopper" <gh@acme.ai>') == "Grace"
    assert reply_first_name("", "omar@inkeep.com") == "Omar"
    assert reply_first_name("", "hello@acme.ai") == ""


def test_draft_threads_under_the_reply(tmp_path):
    settings = _settings(tmp_path)
    template = load_reply_template(settings.reply_template_path)
    draft = build_reply_draft(template, settings, _reply(), "Stefan", "Acme Data Co.")
    assert draft["To"] == "Stefan Weber <stefan@acme.ai>"
    assert draft["Subject"] == "Re: opportunities @ Acme"
    assert draft["In-Reply-To"] == "<reply-1@acme.ai>"
    assert draft["References"] == "<orig-1@example.com> <reply-1@acme.ai>"
    assert draft["List-Unsubscribe"] is None
    plain = draft.get_body(("plain",)).get_content()
    html = draft.get_body(("html",)).get_content()
    assert "Dear Stefan," in plain
    assert "at Acme Data Co. is interesting" in plain
    assert '<a href="https://x.com/someone">https://x.com/someone</a>' in html
    assert "[https://" not in plain


def test_unknown_name_and_company_stay_visible_holes(tmp_path):
    settings = _settings(tmp_path)
    template = load_reply_template(settings.reply_template_path)
    plain = build_reply_draft(template, settings, _reply(), "", "") \
        .get_body(("plain",)).get_content()
    assert "Dear [Name]," in plain and "at [company]" in plain


def test_missing_or_broken_template_turns_drafting_off(tmp_path):
    assert load_reply_template(tmp_path / "nope.txt") is None
    broken = tmp_path / "broken.txt"
    broken.write_text("Subject: hi\n\n{not_a_field}\n", encoding="utf-8")
    assert load_reply_template(broken) is None


class FakeIMAP:
    """Just enough IMAP for scan_inbox: an inbox, a Spam folder and Drafts."""

    def __init__(self, messages, spam=()):
        self.folders = {"INBOX": list(messages), '"[Gmail]/Spam"': list(spam)}
        self.selected = "INBOX"
        self.appended = []
        self.moved = []

    @property
    def messages(self):
        return self.folders[self.selected]

    def __call__(self, *_args):
        return self

    def login(self, *_args):
        return "OK", []

    def select(self, mailbox, *_args):
        self.selected = mailbox
        return "OK", []

    def uid(self, command, *args):
        if command == "SEARCH":
            return "OK", [b" ".join(str(i + 1).encode()
                                    for i in range(len(self.messages)))]
        if command == "FETCH":
            return "OK", [(b"1 (BODY[])", self.messages[int(args[0]) - 1].encode())]
        if command == "MOVE":
            self.moved.append((self.selected, args[0], args[1]))
            return "OK", []
        raise AssertionError(command)

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"',
                      b'(\\HasNoChildren \\Drafts) "/" "[Gmail]/Drafts"',
                      b'(\\HasNoChildren \\Junk) "/" "[Gmail]/Spam"',
                      b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Trash"']

    def append(self, mailbox, flags, _date, message):
        self.appended.append((mailbox, flags, message))
        return "OK", []

    def logout(self):
        return "BYE", []


@pytest.fixture
def db_with_send(tmp_path):
    db = Database(tmp_path / "t.db")
    company_id = db.upsert_company(Company(name="Acme Inc.", domain="acme.ai"))
    contact_id = db.upsert_contact(
        Contact(email="stefan@acme.ai", company_id=company_id, first_name="Stefan"))
    db.record_send(contact_id, "stefan@acme.ai", "opportunities @ Acme",
                   "<orig-1@example.com>", "smtp", "2026-09-14")
    yield db
    db.close()


def test_scan_drafts_one_reply_per_person_and_sends_nothing(tmp_path, monkeypatch,
                                                            db_with_send):
    imap = FakeIMAP([_reply().as_string(), _reply(body="One more thing.").as_string()])
    monkeypatch.setattr(inbox.imaplib, "IMAP4_SSL", imap)
    settings = _settings(tmp_path)

    first = scan_inbox(settings, db_with_send)
    again = scan_inbox(settings, db_with_send)

    assert first.drafted == ["stefan@acme.ai"] and again.drafted == []
    assert len(imap.appended) == 1
    mailbox, flags, raw = imap.appended[0]
    assert mailbox == '"[Gmail]/Drafts"' and "\\Draft" in flags
    assert b"Dear Stefan," in raw


def test_opt_out_reply_still_gets_a_draft(tmp_path, monkeypatch, db_with_send):
    imap = FakeIMAP([_reply(body="Not interested, thanks.").as_string()])
    monkeypatch.setattr(inbox.imaplib, "IMAP4_SSL", imap)
    scan = scan_inbox(_settings(tmp_path), db_with_send)
    assert scan.opted_out == ["stefan@acme.ai"]
    assert scan.drafted == ["stefan@acme.ai"]
    assert db_with_send.is_suppressed("stefan@acme.ai")


def test_out_of_office_gets_no_draft(tmp_path, monkeypatch, db_with_send):
    imap = FakeIMAP([_reply(subject="Automatic reply: opportunities @ Acme").as_string()])
    monkeypatch.setattr(inbox.imaplib, "IMAP4_SSL", imap)
    assert scan_inbox(_settings(tmp_path), db_with_send).drafted == []
    assert imap.appended == []


def test_no_template_means_no_drafts_but_replies_still_recorded(tmp_path, monkeypatch,
                                                                db_with_send):
    imap = FakeIMAP([_reply().as_string()])
    monkeypatch.setattr(inbox.imaplib, "IMAP4_SSL", imap)
    scan = scan_inbox(_settings(tmp_path, template=None), db_with_send)
    assert scan.replied == ["stefan@acme.ai"] and scan.drafted == []
    assert imap.appended == []


def test_failed_save_is_retried_on_the_next_scan(tmp_path, monkeypatch, db_with_send):
    imap = FakeIMAP([_reply().as_string()])
    monkeypatch.setattr(inbox.imaplib, "IMAP4_SSL", imap)
    settings = _settings(tmp_path)
    monkeypatch.setattr(imap, "append", lambda *_a: ("NO", [b"quota"]))
    assert scan_inbox(settings, db_with_send).drafted == []
    monkeypatch.undo()
    monkeypatch.setattr(inbox.imaplib, "IMAP4_SSL", imap)
    assert scan_inbox(settings, db_with_send).drafted == ["stefan@acme.ai"]


BOUNCE_NOTICE = (
    "From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>\n"
    "To: ada@example.com\nSubject: Delivery Status Notification (Failure)\n\n"
    "Final-Recipient: rfc822; stefan@acme.ai\nStatus: 5.1.1\n"
)


def _events(db, kind):
    return db.conn.execute(
        "SELECT ref FROM events WHERE kind = ?", (kind,)).fetchall()


def test_a_bounce_still_in_the_inbox_is_logged_once(tmp_path, monkeypatch,
                                                     db_with_send):
    monkeypatch.setattr(inbox.imaplib, "IMAP4_SSL", FakeIMAP([BOUNCE_NOTICE]))
    settings = _settings(tmp_path)
    first = scan_inbox(settings, db_with_send)
    again = scan_inbox(settings, db_with_send)
    assert first.bounced == [("stefan@acme.ai", True)] and again.bounced == []
    assert len(_events(db_with_send, "bounce")) == 1


DELAY_NOTICE = (
    "From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>\n"
    "To: ada@example.com\nSubject: Delivery Status Notification (Delay)\n\n"
    "Message not delivered yet. Gmail will keep trying for 47 more hours.\n\n"
    "Final-Recipient: rfc822; stefan@acme.ai\nAction: delayed\nStatus: 4.4.1\n"
)


def test_a_delay_notice_is_not_a_bounce(tmp_path, monkeypatch, db_with_send):
    """RFC 3464 "Action: delayed": the server is still trying.

    Every mailer-daemon message was read as a bounce, and anything without
    a hard code as a soft one, so a delay suppressed a reachable person for
    good and counted against the ramp. If delivery does fail, a separate
    final notice arrives and is recorded then.
    """
    monkeypatch.setattr(inbox.imaplib, "IMAP4_SSL", FakeIMAP([DELAY_NOTICE]))
    scan = scan_inbox(_settings(tmp_path), db_with_send)
    assert scan.bounced == []
    assert not db_with_send.is_suppressed("stefan@acme.ai")
    assert db_with_send.find_contact_by_email("stefan@acme.ai")["status"] == "sent"


def test_the_final_failure_after_a_delay_is_still_a_bounce(tmp_path, monkeypatch,
                                                           db_with_send):
    monkeypatch.setattr(inbox.imaplib, "IMAP4_SSL",
                        FakeIMAP([DELAY_NOTICE, BOUNCE_NOTICE]))
    assert scan_inbox(_settings(tmp_path), db_with_send).bounced == [
        ("stefan@acme.ai", True)]


def test_a_recorded_bounce_notice_is_binned(tmp_path, monkeypatch, db_with_send):
    imap = FakeIMAP([BOUNCE_NOTICE])
    monkeypatch.setattr(inbox.imaplib, "IMAP4_SSL", imap)
    scan = scan_inbox(_settings(tmp_path), db_with_send)
    assert scan.trashed == 1
    assert imap.moved == [("INBOX", b"1", '"[Gmail]/Trash"')]
    assert db_with_send.is_suppressed("stefan@acme.ai")


def test_a_bounce_already_recorded_is_still_binned(tmp_path, monkeypatch, db_with_send):
    imap = FakeIMAP([BOUNCE_NOTICE, BOUNCE_NOTICE])
    monkeypatch.setattr(inbox.imaplib, "IMAP4_SSL", imap)
    assert scan_inbox(_settings(tmp_path), db_with_send).trashed == 2


def test_bounces_about_strangers_and_delays_stay_put(tmp_path, monkeypatch, db_with_send):
    """The main account is a personal mailbox too: only our own bounces go."""
    stranger = BOUNCE_NOTICE.replace("stefan@acme.ai", "aunt@family.example")
    imap = FakeIMAP([stranger, DELAY_NOTICE])
    monkeypatch.setattr(inbox.imaplib, "IMAP4_SSL", imap)
    assert scan_inbox(_settings(tmp_path), db_with_send).trashed == 0
    assert imap.moved == []


def test_an_opt_out_still_in_the_inbox_is_logged_once(tmp_path, monkeypatch,
                                                       db_with_send):
    imap = FakeIMAP([_reply(body="Not interested, thanks.").as_string()])
    monkeypatch.setattr(inbox.imaplib, "IMAP4_SSL", imap)
    settings = _settings(tmp_path)
    assert scan_inbox(settings, db_with_send).opted_out == ["stefan@acme.ai"]
    assert scan_inbox(settings, db_with_send).opted_out == []
    assert len(_events(db_with_send, "opt_out")) == 1


def test_drafts_folder_falls_back_without_special_use():
    conn = SimpleNamespace(list=lambda: ("OK", [b'(\\HasNoChildren) "/" "INBOX"']))
    assert drafts_mailbox(conn) == "Drafts"


def test_reply_from_a_colleague_in_our_thread_counts(tmp_path, monkeypatch,
                                                      db_with_send):
    """gc@ answering the note sent to git@ must close the company."""
    imap = FakeIMAP([_reply(sender="Giuseppe <gc@acme.ai>").as_string()])
    monkeypatch.setattr(inbox.imaplib, "IMAP4_SSL", imap)
    scan = scan_inbox(_settings(tmp_path), db_with_send)
    assert scan.replied == ["stefan@acme.ai"]
    assert db_with_send.find_contact_by_email("stefan@acme.ai")["status"] == "replied"


def test_the_list_unsubscribe_mail_is_an_opt_out(tmp_path, monkeypatch, db_with_send):
    """What the unsubscribe link in the mail client actually sends.

    Every message carries List-Unsubscribe: <mailto:...?subject=unsubscribe>.
    The client mails that address with the subject "unsubscribe" and a body
    of its own boilerplate. Only the body was read, so the one-click opt-out
    was recorded as a reply and answered with a friendly draft.
    """
    msg = ("From: Stefan Weber <stefan@acme.ai>\nTo: ada@example.com\n"
           "Subject: unsubscribe\nMessage-ID: <u-1@acme.ai>\n\n"
           "This message was automatically generated by Gmail.\n")
    imap = FakeIMAP([msg])
    monkeypatch.setattr(inbox.imaplib, "IMAP4_SSL", imap)
    scan = scan_inbox(_settings(tmp_path), db_with_send)
    assert scan.opted_out == ["stefan@acme.ai"] and scan.replied == []
    assert db_with_send.is_suppressed("stefan@acme.ai")


def test_a_subject_naming_our_subject_is_not_an_opt_out(tmp_path, monkeypatch,
                                                        db_with_send):
    imap = FakeIMAP([_reply(subject="Re: <30 seconds; opportunities @ Acme",
                            body="Happy to chat.").as_string()])
    monkeypatch.setattr(inbox.imaplib, "IMAP4_SSL", imap)
    scan = scan_inbox(_settings(tmp_path), db_with_send)
    assert scan.replied == ["stefan@acme.ai"] and scan.opted_out == []


def _fresh(sender, body, extra=""):
    """A new message, not a reply: no In-Reply-To, no References."""
    return (f"From: {sender}\nTo: ada@example.com\nSubject: your emails\n"
            f"Message-ID: <fresh-1@acme.ai>\n{extra}\n{body}\n")


def test_a_colleague_writing_fresh_to_say_stop_closes_the_company(
        tmp_path, monkeypatch, db_with_send):
    """No thread headers, so neither the sender nor the thread is known.

    A colleague forwarding nothing and writing "stop emailing our team" from
    the company's own domain was dropped, and the follow-up to Stefan went
    out a week later.
    """
    imap = FakeIMAP([_fresh("Maria Rossi <maria@acme.ai>",
                            "Please stop emailing our team.")])
    monkeypatch.setattr(inbox.imaplib, "IMAP4_SSL", imap)
    scan = scan_inbox(_settings(tmp_path), db_with_send)
    assert scan.opted_out == ["stefan@acme.ai"]
    assert db_with_send.is_suppressed("stefan@acme.ai")
    assert db_with_send.is_suppressed("maria@acme.ai")
    assert db_with_send.company_closed(
        db_with_send.find_contact_by_email("stefan@acme.ai")["company_id"])
    assert imap.appended == [], "no drafted reply greeting the wrong person"
    assert scan_inbox(_settings(tmp_path), db_with_send).opted_out == []
    assert len(_events(db_with_send, "opt_out")) == 1


def test_a_newsletter_from_a_contacted_domain_is_not_an_opt_out(
        tmp_path, monkeypatch, db_with_send):
    imap = FakeIMAP([_fresh("Acme News <news@acme.ai>",
                            "Our launch week recap. Unsubscribe here.",
                            extra="List-Unsubscribe: <mailto:u@acme.ai>")])
    monkeypatch.setattr(inbox.imaplib, "IMAP4_SSL", imap)
    scan = scan_inbox(_settings(tmp_path), db_with_send)
    assert scan.opted_out == []
    assert not db_with_send.is_suppressed("stefan@acme.ai")


def test_a_fresh_stop_from_a_freemail_stranger_closes_nothing(
        tmp_path, monkeypatch, db_with_send):
    imap = FakeIMAP([_fresh("someone@gmail.com", "stop emailing me")])
    monkeypatch.setattr(inbox.imaplib, "IMAP4_SSL", imap)
    assert scan_inbox(_settings(tmp_path), db_with_send).opted_out == []
    assert not db_with_send.is_suppressed("stefan@acme.ai")


def test_unrelated_mail_from_a_stranger_is_ignored(tmp_path, monkeypatch,
                                                   db_with_send):
    msg = email_lib.message_from_string(
        "From: news@other.com\nTo: ada@example.com\nSubject: Weekly\n"
        "Message-ID: <n-1@other.com>\n\nHello\n")
    imap = FakeIMAP([msg.as_string()])
    monkeypatch.setattr(inbox.imaplib, "IMAP4_SSL", imap)
    scan = scan_inbox(_settings(tmp_path), db_with_send)
    assert scan.replied == [] and scan.drafted == []


# ------------------------------------------------------------ the Spam folder

NEWSLETTER = ("From: Shop <deals@shop.example>\nTo: ada@example.com\n"
              "Subject: Big sale\nMessage-ID: <n-1@shop.example>\n\nBuy now.\n")


def test_reply_in_spam_counts_and_is_moved_to_the_inbox(tmp_path, monkeypatch,
                                                         db_with_send):
    imap = FakeIMAP([], spam=[NEWSLETTER, _reply().as_string()])
    monkeypatch.setattr(inbox.imaplib, "IMAP4_SSL", imap)
    scan = scan_inbox(_settings(tmp_path), db_with_send, spam=True)
    assert scan.replied == ["stefan@acme.ai"]
    assert scan.rescued == ["stefan@acme.ai"]
    assert scan.drafted == ["stefan@acme.ai"]
    assert imap.moved == [('"[Gmail]/Spam"', b"2", "INBOX")]


def test_unsubscribe_in_spam_is_an_opt_out_not_a_reply(tmp_path, monkeypatch,
                                                       db_with_send):
    imap = FakeIMAP([], spam=[_reply(subject="unsubscribe", body="unsubscribe").as_string()])
    monkeypatch.setattr(inbox.imaplib, "IMAP4_SSL", imap)
    scan = scan_inbox(_settings(tmp_path), db_with_send, spam=True)
    assert scan.replied == [] and scan.opted_out == ["stefan@acme.ai"]
    assert db_with_send.is_suppressed("stefan@acme.ai")


def test_inbox_scan_never_reads_spam(tmp_path, monkeypatch, db_with_send):
    imap = FakeIMAP([], spam=[_reply().as_string()])
    monkeypatch.setattr(inbox.imaplib, "IMAP4_SSL", imap)
    scan = scan_inbox(_settings(tmp_path), db_with_send)
    assert scan.replied == [] and imap.moved == []
