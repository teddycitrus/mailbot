"""Apollo exports go through the same gates as every other source.

Apollo marks an address Verified on a catch-all domain too, so our own probe
decides: verified joins the open queue, catch-all only reaches the volume
lane, and only when Apollo itself vouched for it. All addresses are synthetic.
"""

from __future__ import annotations

import csv
from types import SimpleNamespace

from src.apollo import import_export, same_company, site_domain
from src.database import Database
from src.models import CATCHALL, UNDELIVERABLE, VERIFIED, VOLUME_LANE
from src.verifier import Verdict

HEADER = ["First Name", "Last Name", "Title", "Company Name", "Email", "Email Status",
          "# Employees", "Industry", "Keywords", "Person Linkedin Url", "Website",
          "Company City", "Company State", "Company Country"]


def row(first, email, status="Verified", employees="40", website="https://www.acme.test"):
    return [first, "Lee", "Founder", "Acme", email, status, employees, "software",
            "ml tooling", "", website, "San Francisco", "California", "United States"]


class FakeVerifier:
    def __init__(self, verdicts):
        self.verdicts = verdicts

    def verify(self, email, source=None):
        return self.verdicts.get(email, Verdict(UNDELIVERABLE, "rejected 550", 0))


SETTINGS = SimpleNamespace(max_contacts_per_company=3, max_employees=200,
                           volume_guesses=True)


def write_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADER)
        writer.writerows(rows)


def setup(tmp_path):
    path = tmp_path / "apollo.csv"
    write_csv(path, [
        row("Ann", "ann@acme.test"),
        row("Bo", "bo@catchy.test", website="catchy.test"),
        row("Cy", "cy@catchy.test", status="Guessed", website="catchy.test"),
        row("Di", "di@huge.test", employees="3200", website="huge.test"),
        row("Ed", "ed@other.test"),
        row("Fay", "", status="Unavailable"),
        row("Gus", "hello@acme.test"),
        row("Hal", "hal@acme.test"),
    ])
    verifier = FakeVerifier({
        "ann@acme.test": Verdict(VERIFIED, "mailbox accepted", 90),
        "bo@catchy.test": Verdict(CATCHALL, "catch-all domain, address was guessed", 25),
        "cy@catchy.test": Verdict(CATCHALL, "catch-all domain, address was guessed", 25),
        "di@huge.test": Verdict(VERIFIED, "mailbox accepted", 90),
    })
    return path, verifier


def test_only_our_own_verdict_decides_and_catchalls_go_to_the_volume_lane(tmp_path):
    path, verifier = setup(tmp_path)
    db = Database(tmp_path / "o.db")
    report = import_export(path, db, verifier, SETTINGS)
    assert (report.rows, report.added, report.reserved) == (8, 2, 1)
    ann, bo = db.find_contact_by_email("ann@acme.test"), db.find_contact_by_email("bo@catchy.test")
    assert (ann["verify_status"], ann["lane"], ann["provenance"]) == (VERIFIED, "", "apollo")
    assert (bo["verify_status"], bo["lane"]) == (CATCHALL, VOLUME_LANE)
    for skipped in ("cy@catchy.test", "di@huge.test", "ed@other.test",
                    "hello@acme.test", "hal@acme.test"):
        assert db.find_contact_by_email(skipped) is None, skipped
    why = {email: detail for email, state, detail in report.lines if state == "skip"}
    assert "nothing to prove it with" in why["cy@catchy.test"]
    assert "MAX_EMPLOYEES" in why["di@huge.test"]
    assert "not on the company site's domain" in why["ed@other.test"]
    assert why["hello@acme.test"] == "shared or blocked inbox"
    # The company carries its location, so the send window follows it.
    assert "San Francisco" in db.get_company(ann["company_id"])["location"]
    db.close()


def test_without_volume_guesses_a_catchall_is_never_kept(tmp_path):
    path, verifier = setup(tmp_path)
    db = Database(tmp_path / "o.db")
    off = SimpleNamespace(**{**SETTINGS.__dict__, "volume_guesses": False})
    report = import_export(path, db, verifier, off)
    assert report.reserved == 0 and db.find_contact_by_email("bo@catchy.test") is None
    db.close()


def test_a_dry_run_writes_nothing(tmp_path):
    path, verifier = setup(tmp_path)
    db = Database(tmp_path / "o.db")
    report = import_export(path, db, verifier, SETTINGS, dry_run=True)
    assert report.added == 2
    assert db.find_contact_by_email("ann@acme.test") is None
    db.close()


def test_any_size_lifts_only_the_size_check(tmp_path):
    path, verifier = setup(tmp_path)
    db = Database(tmp_path / "o.db")
    import_export(path, db, verifier, SETTINGS, any_size=True)
    assert db.find_contact_by_email("di@huge.test") is not None
    db.close()


def test_domains_are_matched_the_way_apollo_writes_them():
    assert site_domain("https://www.x.ai") == "x.ai"
    assert site_domain("majestic-labs.ai") == "majestic-labs.ai"
    assert same_company("x.ai", "x.ai")
    assert same_company("eu.acme.test", "acme.test")
    assert not same_company("gmail.com", "acme.test")
    assert not same_company("acme.test", "")
