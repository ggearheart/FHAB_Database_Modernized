"""Tests for entering a report as a user, under RLS."""

import psycopg
import pytest

from fhab.auth import create_user, grant_role
from fhab.reports import enter_report

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
