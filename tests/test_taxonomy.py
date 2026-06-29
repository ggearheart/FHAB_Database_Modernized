"""Tests for analyte taxonomy admin: edit, merge aliases (repoint results), delete."""

import pytest

from fhab.auth import create_user, grant_role, set_password
from fhab.reports import add_result, enter_report
from fhab.taxonomy import (TaxonomyError, delete_analyte, list_analytes, merge_analytes,
                           update_analyte)

R5 = "Region 5 - Central Valley"


def _analyte(conn, analysis_type, analyte_class, analyte, unit="ug/L"):
    return conn.execute(
        """INSERT INTO analyte (analysis_type, analyte_class, analyte, default_unit)
           VALUES (%s,%s,%s,%s) RETURNING id""",
        (analysis_type, analyte_class, analyte, unit)).fetchone()["id"]


def _staff(conn):
    u = create_user(conn, "lab@wb.ca.gov"); grant_role(conn, u, "wb_staff", region=R5)
    return u


def test_merge_repoints_results_and_removes_alias(conn):
    staff = _staff(conn)
    canon = _analyte(conn, "Cyanotoxin", "Microcystins", "Microcystins")
    alias = _analyte(conn, "Cyanotoxin", "Microcystins", "mcyE")
    conn.commit()
    brid = enter_report(conn, staff, water_body_name="Clear Lake", region=R5)
    add_result(conn, staff, brid, data_type="Laboratory", analyte_id=alias, measurement_value=5)
    add_result(conn, staff, brid, data_type="Laboratory", analyte_id=alias, measurement_value=8)

    moved = merge_analytes(conn, alias, canon)
    assert moved == 2
    assert conn.execute("SELECT 1 FROM analyte WHERE id=%s", (alias,)).fetchone() is None
    n = conn.execute("SELECT count(*) c FROM result WHERE analyte_id=%s", (canon,)).fetchone()["c"]
    assert n == 2


def test_merge_validations(conn):
    a = _analyte(conn, "Cyanotoxin", "Anatoxins", "anaC"); conn.commit()
    with pytest.raises(TaxonomyError):
        merge_analytes(conn, a, a)            # into itself
    with pytest.raises(TaxonomyError):
        merge_analytes(conn, a, 999999)       # missing target


def test_update_analyte_and_collision(conn):
    a = _analyte(conn, "Cyanotoxin", "Cylindrospermopsin", "cyl")
    _analyte(conn, "Cyanotoxin", "Cylindrospermopsin", "cyl_canon"); conn.commit()
    update_analyte(conn, a, analysis_type="Cyanotoxin", analyte_class="Cylindrospermopsin",
                   analyte="CYL", default_unit="ug/L")
    assert conn.execute("SELECT analyte FROM analyte WHERE id=%s", (a,)).fetchone()["analyte"] == "CYL"
    # renaming to collide with the other's exact (type, class, name) is rejected — merge instead.
    with pytest.raises(TaxonomyError):
        update_analyte(conn, a, analysis_type="Cyanotoxin", analyte_class="Cylindrospermopsin",
                       analyte="cyl_canon")


def test_delete_unused_only(conn):
    staff = _staff(conn)
    used = _analyte(conn, "Cyanotoxin", "Saxitoxin", "stx_used")
    unused = _analyte(conn, "Cyanotoxin", "Saxitoxin", "stx_unused"); conn.commit()
    brid = enter_report(conn, staff, water_body_name="Lake", region=R5)
    add_result(conn, staff, brid, data_type="Laboratory", analyte_id=used, measurement_value=1)
    with pytest.raises(TaxonomyError):
        delete_analyte(conn, used)
    delete_analyte(conn, unused)
    assert conn.execute("SELECT 1 FROM analyte WHERE id=%s", (unused,)).fetchone() is None


# --- web ---

@pytest.fixture()
def client(conn):
    from tests.conftest import TEST_DSN
    from fhab.web import create_app
    admin = create_user(conn, "admin@fhab.local", "Admin")
    set_password(conn, admin, "pw"); grant_role(conn, admin, "program_admin")
    app = create_app(dsn=TEST_DSN); app.config["TESTING"] = True
    return app.test_client()


def test_analytes_screen_and_merge_via_web(client, conn):
    canon = _analyte(conn, "Cyanotoxin", "Microcystins", "Microcystins")
    alias = _analyte(conn, "Cyanotoxin", "Microcystins", "Microcystins total"); conn.commit()
    client.post("/login", data={"email": "admin@fhab.local", "password": "pw"}, follow_redirects=True)
    r = client.get("/admin/analytes")
    assert r.status_code == 200 and b"Microcystins total" in r.data
    r = client.post(f"/admin/analytes/{alias}/merge", data={"target_id": str(canon)},
                    follow_redirects=True)
    assert b"Merged" in r.data
    assert conn.execute("SELECT 1 FROM analyte WHERE id=%s", (alias,)).fetchone() is None


def test_analytes_requires_admin(client, conn):
    staff = create_user(conn, "s@wb.ca.gov"); grant_role(conn, staff, "wb_staff", region=R5)
    set_password(conn, staff, "pw")
    client.post("/login", data={"email": "s@wb.ca.gov", "password": "pw"}, follow_redirects=True)
    assert client.get("/admin/analytes", follow_redirects=True).status_code in (200, 403)
    assert b"Microcystins" not in client.get("/admin/analytes", follow_redirects=True).data
