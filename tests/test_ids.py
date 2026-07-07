"""Governance #2: locally-authored ids come from a reserved range (>= 1e9) and never collide
with the smaller published/legacy ids that imports insert explicitly."""

from fhab.auth import create_user, grant_role
from fhab.cases import create_case
from fhab.reports import add_response, enter_report

R5 = "Region 5 - Central Valley"
RESERVED = 1_000_000_000


def _staff(conn):
    u = create_user(conn, "ids@wb.ca.gov"); grant_role(conn, u, "wb_staff", region=R5)
    return u


def test_app_ids_are_in_reserved_range_and_unique(conn):
    staff = _staff(conn)
    r1 = enter_report(conn, staff, water_body_name="A", region=R5)
    r2 = enter_report(conn, staff, water_body_name="B", region=R5)
    assert r1 >= RESERVED and r2 >= RESERVED and r1 != r2         # no race, reserved range
    cid = create_case(conn, staff, water_body_name="C", region=R5)
    assert cid >= RESERVED
    rid = add_response(conn, staff, r1, response_category="Advisory",
                       advisory_recommended="Caution", display_advisory_on_map=True)
    assert rid >= RESERVED
    adv = conn.execute("SELECT advisory_id FROM advisory WHERE response_action_id=%s", (rid,)).fetchone()
    assert adv["advisory_id"] >= RESERVED


def test_imported_low_id_coexists_and_never_conflated(conn):
    staff = _staff(conn)
    app_r = enter_report(conn, staff, water_body_name="App", region=R5)   # reserved >= 1e9
    # An import (open-data loader / data.ca.gov refresh) inserts a published report at a low id,
    # exactly as those paths do — this must NOT touch or collide with the app-created report.
    conn.execute("INSERT INTO event (bloom_report_id, report_type) VALUES (5, 'Imported')")
    conn.commit()
    assert conn.execute("SELECT 1 FROM event WHERE bloom_report_id=5").fetchone()
    assert conn.execute("SELECT report_type FROM event WHERE bloom_report_id=%s",
                        (app_r,)).fetchone()["report_type"] != "Imported"    # not overwritten
    # the next app report is still in the reserved range, above the low import
    app_r2 = enter_report(conn, staff, water_body_name="App2", region=R5)
    assert app_r2 >= RESERVED and app_r2 not in (5, app_r)
