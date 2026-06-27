"""Tests for entering a report as a user, under RLS."""

import psycopg
import pytest

from fhab.auth import create_user, grant_role
from fhab.reports import add_response, add_result, enter_report, update_report

R5 = "Region 5 - Central Valley"


def test_staff_enters_report_in_region(conn):
    staff = create_user(conn, "filer@wb.ca.gov"); grant_role(conn, staff, "wb_staff", region=R5)
    rid = enter_report(conn, staff, water_body_name="Demo Pond", region=R5,
                       county="Sacramento", lat=38.58, lon=-121.49, bloom_type="cyanobacteria")
    assert isinstance(rid, int)
    row = conn.execute(
        """SELECT w.water_body_name, w.regional_water_board, e.bloom_type
           FROM event e JOIN location l ON l.id=e.location_id JOIN waterbody w ON w.id=l.waterbody_id
           WHERE e.bloom_report_id=%s""", (rid,)).fetchone()
    assert row["water_body_name"] == "Demo Pond"
    assert row["regional_water_board"] == R5
    assert row["bloom_type"] == "cyanobacteria"


def test_staff_enters_report_on_behalf_of_other_region(conn):
    # A regional staffer may file a report for a different region (the CLI warns + confirms).
    staff = create_user(conn, "r2@wb.ca.gov")
    grant_role(conn, staff, "wb_staff", region="Region 2 - San Francisco Bay")
    rid = enter_report(conn, staff, water_body_name="Other Region Pond",
                       region=R5, county="Lake", lat=39.0, lon=-122.9)
    # Visible to the owning region's staff (and admin), confirming it landed in Region 5.
    region = conn.execute(
        """SELECT w.regional_water_board FROM event e JOIN location l ON l.id=e.location_id
           JOIN waterbody w ON w.id=l.waterbody_id WHERE e.bloom_report_id=%s""", (rid,)
    ).fetchone()["regional_water_board"]
    assert region == R5


def test_report_records_determination(conn):
    staff = create_user(conn, "det@wb.ca.gov"); grant_role(conn, staff, "wb_staff", region=R5)
    rid = enter_report(conn, staff, water_body_name="Determination Pond", region=R5,
                       determination="confirmed_hab")
    code = conn.execute(
        "SELECT determination_code FROM event WHERE bloom_report_id=%s", (rid,)).fetchone()
    assert code["determination_code"] == "confirmed_hab"


def test_update_report_edits_fields(conn):
    staff = create_user(conn, "ed@wb.ca.gov"); grant_role(conn, staff, "wb_staff", region=R5)
    rid = enter_report(conn, staff, water_body_name="Edit Pond", region=R5)
    update_report(conn, staff, rid, bloom_type="cyanobacteria", determination="confirmed_hab",
                  bloom_description="Verified in the field.")
    row = conn.execute(
        "SELECT bloom_type, determination_code, bloom_description FROM event WHERE bloom_report_id=%s",
        (rid,)).fetchone()
    assert row["bloom_type"] == "cyanobacteria"
    assert row["determination_code"] == "confirmed_hab"


def test_add_result_records_sample_and_result(conn):
    staff = create_user(conn, "lab@wb.ca.gov"); grant_role(conn, staff, "wb_staff", region=R5)
    rid = enter_report(conn, staff, water_body_name="Lab Pond", region=R5)
    analyte_id = conn.execute(
        "SELECT id FROM analyte WHERE analyte='Microcystin' LIMIT 1").fetchone()["id"]
    add_result(conn, staff, rid, data_type="Laboratory", analyte_id=analyte_id,
               measurement_value=12.5, measurement_unit="ug/L", method="ELISA",
               sample_label="S-001", sample_date="2026-06-20")
    row = conn.execute(
        """SELECT r.data_type, r.measurement_value, an.analyte, s.sample_id
           FROM result r JOIN sample s ON s.id=r.sample_id JOIN analyte an ON an.id=r.analyte_id
           WHERE s.bloom_report_id=%s""", (rid,)).fetchone()
    assert row["data_type"] == "Laboratory"
    assert float(row["measurement_value"]) == 12.5
    assert row["analyte"] == "Microcystin"
    assert row["sample_id"] == "S-001"


def test_add_response_posts_advisory(conn):
    staff = create_user(conn, "adv@wb.ca.gov"); grant_role(conn, staff, "wb_staff", region=R5)
    rid = enter_report(conn, staff, water_body_name="Advisory Lake", region=R5)
    rsp = add_response(conn, staff, rid, response_category="Advisory",
                       advisory_recommended="Danger", display_advisory_on_map=True,
                       advisory_detail="Toxins detected")
    row = conn.execute(
        """SELECT a.advisory_recommended, a.display_advisory_on_map, r.response_category
           FROM response r JOIN advisory a ON a.response_action_id = r.response_action_id
           WHERE r.response_action_id = %s""", (rsp,)).fetchone()
    assert row["advisory_recommended"] == "Danger"
    assert row["display_advisory_on_map"] is True
    assert row["response_category"] == "Advisory"


def test_response_without_advisory(conn):
    staff = create_user(conn, "inv@wb.ca.gov"); grant_role(conn, staff, "wb_staff", region=R5)
    rid = enter_report(conn, staff, water_body_name="Investigation Lake", region=R5)
    rsp = add_response(conn, staff, rid, response_category="Investigation")
    n_adv = conn.execute(
        "SELECT count(*) c FROM advisory WHERE response_action_id=%s", (rsp,)).fetchone()["c"]
    assert n_adv == 0  # no advisory created when none recommended


def test_contributor_cannot_post_advisory(conn):
    import psycopg
    import pytest
    user = create_user(conn, "c2@tribe.org"); grant_role(conn, user, "tribal_admin", org="TribeX")
    # Seed an event owned by the contributor org so they can reference it.
    wb = conn.execute("INSERT INTO waterbody (water_body_name) VALUES ('TC') RETURNING id").fetchone()["id"]
    loc = conn.execute("INSERT INTO location (waterbody_id) VALUES (%s) RETURNING id", (wb,)).fetchone()["id"]
    conn.execute("INSERT INTO event (bloom_report_id, location_id, owner_org) VALUES (95001,%s,'TribeX')", (loc,))
    conn.commit()
    with pytest.raises(psycopg.Error):
        add_response(conn, user, 95001, advisory_recommended="Caution")
    conn.rollback()


def test_determination_vocabulary_seeded(conn):
    codes = {r["code"] for r in conn.execute("SELECT code FROM report_determination").fetchall()}
    assert {"confirmed_hab", "red_tide", "non_hab_algae", "spill", "other_wq"} <= codes


def test_public_user_cannot_enter_report(conn):
    pub = create_user(conn, "p@public.org"); grant_role(conn, pub, "public")
    with pytest.raises(psycopg.Error):
        enter_report(conn, pub, water_body_name="Nope Pond", region=R5)
    conn.rollback()


def test_contributor_cannot_create_report_geography(conn):
    # Report intake (new waterbody/location) is staff-only; a contributor's path is
    # stations/samples/results (owner_org). So enter_report is rejected for a contributor.
    user = create_user(conn, "mon@tribe.org"); grant_role(conn, user, "tribal_admin", org="TribeX")
    with pytest.raises(psycopg.Error):
        enter_report(conn, user, water_body_name="Tribal Creek", lat=40.1, lon=-123.7,
                     owner_org="TribeX")
    conn.rollback()
