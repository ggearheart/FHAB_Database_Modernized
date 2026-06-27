"""Tests for case-management helpers (under RLS)."""

import psycopg
import pytest

from fhab.auth import create_user, grant_role
from fhab.cases import assign_report_to_case, create_case, update_case
from fhab.reports import enter_report

R5 = "Region 5 - Central Valley"
R1 = "Region 1 - North Coast"


def _staff(conn, email="cm@wb.ca.gov", region=R5):
    u = create_user(conn, email)
    grant_role(conn, u, "wb_staff", region=region)
    return u


def test_create_and_assign_case(conn):
    staff = _staff(conn)
    cid = create_case(conn, staff, water_body_name="Case Lake", region=R5, year=2026,
                      case_class="Event Response", case_lead="J. Rivera")
    case = conn.execute("SELECT case_status, case_year, case_lead FROM hab_case WHERE case_id=%s",
                        (cid,)).fetchone()
    assert case["case_status"] == "Open" and case["case_year"] == 2026

    rid = enter_report(conn, staff, water_body_name="Case Lake", region=R5)
    assign_report_to_case(conn, staff, rid, cid)
    assert conn.execute("SELECT case_id FROM event WHERE bloom_report_id=%s", (rid,)).fetchone()["case_id"] == cid


def test_update_case_closes_with_end_date(conn):
    staff = _staff(conn)
    cid = create_case(conn, staff, water_body_name="Close Lake", region=R5)
    update_case(conn, staff, cid, status="Closed", case_lead="K. Lee")
    row = conn.execute("SELECT case_status, case_end_date FROM hab_case WHERE case_id=%s", (cid,)).fetchone()
    assert row["case_status"] == "Closed" and row["case_end_date"] is not None


def test_case_creation_is_region_scoped(conn):
    # A Region-5 staffer cannot create a case for a Region-1 waterbody.
    staff = _staff(conn, "r5cm@wb.ca.gov", region=R5)
    with pytest.raises(psycopg.Error):
        create_case(conn, staff, water_body_name="Far Lake", region=R1)
    conn.rollback()


def test_load_chemistry_for_case(conn):
    from pathlib import Path
    from fhab.ceden import load_chemistry_for_case
    staff = _staff(conn)
    cid = create_case(conn, staff, water_body_name="Lab Case Lake", region=R5)
    chem = Path(__file__).parent / "fixtures" / "ceden" / "CEDEN_WaterChemistry.csv"
    rep = load_chemistry_for_case(conn, cid, chem, staff)
    assert rep.counts["results"] == 16
    n = conn.execute("SELECT count(*) c FROM sample WHERE case_id=%s", (cid,)).fetchone()["c"]
    assert n == 4
