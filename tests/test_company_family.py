"""One company, two rows: a YC company that changed domain.

Found live: Normal sat under normalfactory.com and normal.ai with the same
YC page, so each founder had two queued guesses. Rows that share a YC page
are one company for every per-company rule, and one name across them is one
person. All addresses here are synthetic.
"""

from __future__ import annotations

import pytest

from src.database import Database
from src.models import CATCHALL, Company, Contact, PENDING, PROV_GUESS, VOLUME_LANE

YC = "https://www.ycombinator.com/companies/normal"


@pytest.fixture
def twins(tmp_path):
    db = Database(tmp_path / "o.db")
    a = db.upsert_company(Company(name="Normal", domain="old.test", yc_url=YC))
    # Inserted the way the old code did, bypassing the new yc_url check.
    db.conn.execute(
        "INSERT INTO companies (name, domain, yc_url, created_at) VALUES "
        "('Normal', 'new.test', ?, '2026-01-02')", (YC,))
    db.conn.commit()
    b = db.conn.execute("SELECT id FROM companies WHERE domain = 'new.test'").fetchone()[0]
    yield db, a, b
    db.close()


def person(db, cid, email, first="Anson", last="Yu"):
    return db.upsert_contact(Contact(
        email=email, company_id=cid, first_name=first, last_name=last,
        verify_status=CATCHALL, provenance=PROV_GUESS, lane=VOLUME_LANE,
        status=PENDING, confidence=40))


def test_a_new_domain_for_a_known_yc_page_is_the_same_company(tmp_path):
    db = Database(tmp_path / "o.db")
    a = db.upsert_company(Company(name="Normal", domain="old.test", yc_url=YC))
    assert db.upsert_company(Company(name="Normal", domain="new.test", yc_url=YC)) == a
    db.close()


def test_one_person_across_both_rows_is_written_to_once(twins):
    db, a, b = twins
    person(db, a, "anson@old.test")
    person(db, b, "anson@new.test")
    assert db.can_send_to("anson@old.test", VOLUME_LANE) == (True, "")
    assert db.can_send_to("anson@new.test", VOLUME_LANE) == (
        False, "same person as anson@old.test")


def test_a_colleague_on_the_other_row_is_still_a_different_person(twins):
    db, a, b = twins
    person(db, a, "anson@old.test")
    person(db, b, "hud@new.test", first="Hudhayfa", last="Nazoordeen")
    assert db.can_send_to("hud@new.test", VOLUME_LANE) == (True, "")


def test_per_company_rules_span_both_rows(twins):
    db, a, b = twins
    person(db, a, "anson@old.test")
    db.conn.execute("UPDATE contacts SET status = 'replied' WHERE email = 'anson@old.test'")
    db.conn.commit()
    assert db.company_closed(b) == "someone there already replied"
    assert db.company_key(a) == db.company_key(b) == a
    assert db.company_family(b) == [a, b]
