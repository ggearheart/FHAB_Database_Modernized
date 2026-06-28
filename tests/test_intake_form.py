"""Tests for the official-form intake fields: vocabularies, illness matrix, reporter, photos."""

from fhab.auth import create_user, grant_role
from fhab.reports import enter_report, set_report_illness, update_report

R5 = "Region 5 - Central Valley"


def _staff(conn, email="intake@wb.ca.gov"):
    u = create_user(conn, email)
    grant_role(conn, u, "wb_staff", region=R5)
    return u


def test_enter_report_persists_official_fields(conn):
    staff = _staff(conn)
    rid = enter_report(
        conn, staff, water_body_name="Vocab Lake", region=R5, landmark="North boat ramp",
        report_type="Public Reporting", bloom_size="smaller than a sedan",
        bloom_location="<10 feet from shore", bloom_textures=["Surface scum", "Streaking"],
        surface_water_condition="Calm", weather_condition="Clear", signs_posted="Caution",
        has_pictures=True, reporter_name="Jane Doe", reporter_email="jane@example.com",
        reporter_phone="555-1212", reporter_org="Lake Assoc.", management_comments="follow up")
    row = conn.execute(
        """SELECT bloom_size, bloom_location, bloom_textures, surface_water_condition,
                  weather_condition, signs_posted, has_pictures, reporter_name, reporter_email,
                  management_comments, l.landmark
           FROM event e JOIN location l ON l.id = e.location_id
           WHERE e.bloom_report_id = %s""", (rid,)).fetchone()
    assert row["bloom_size"] == "smaller than a sedan"
    assert row["bloom_textures"] == ["Surface scum", "Streaking"]
    assert row["signs_posted"] == "Caution" and row["has_pictures"] is True
    assert row["reporter_name"] == "Jane Doe" and row["landmark"] == "North boat ramp"


def test_illness_matrix_stores_only_marked_subjects(conn):
    staff = _staff(conn, "ill@wb.ca.gov")
    rid = enter_report(conn, staff, water_body_name="Illness Lake", region=R5)
    set_report_illness(conn, staff, rid, rows=[
        {"subject": "Dog", "illness": True, "death": True},
        {"subject": "Human", "illness": True, "death": False},
        {"subject": "Fish", "illness": False, "death": False},  # dropped
    ], none_observed=False, description="two dogs sick")
    rows = conn.execute(
        "SELECT subject, illness, death FROM report_illness WHERE bloom_report_id=%s ORDER BY subject",
        (rid,)).fetchall()
    assert [(r["subject"], r["illness"], r["death"]) for r in rows] == [
        ("Dog", True, True), ("Human", True, False)]
    assert conn.execute("SELECT illness_description FROM event WHERE bloom_report_id=%s",
                        (rid,)).fetchone()["illness_description"] == "two dogs sick"


def test_set_illness_is_idempotent_replace(conn):
    staff = _staff(conn, "ill2@wb.ca.gov")
    rid = enter_report(conn, staff, water_body_name="Replace Lake", region=R5)
    set_report_illness(conn, staff, rid, rows=[{"subject": "Dog", "illness": True, "death": False}])
    set_report_illness(conn, staff, rid, rows=[{"subject": "Cattle", "illness": False, "death": True}])
    rows = conn.execute(
        "SELECT subject FROM report_illness WHERE bloom_report_id=%s", (rid,)).fetchall()
    assert [r["subject"] for r in rows] == ["Cattle"]  # prior Dog row replaced


def test_update_report_sets_vocab_fields(conn):
    staff = _staff(conn, "upd@wb.ca.gov")
    rid = enter_report(conn, staff, water_body_name="Edit Lake", region=R5)
    update_report(conn, staff, rid, bloom_size="no bloom", signs_posted="Danger",
                  bloom_textures=["Benthic mats"], has_pictures=False,
                  management_comments="closed out")
    row = conn.execute(
        "SELECT bloom_size, signs_posted, bloom_textures, has_pictures, management_comments "
        "FROM event WHERE bloom_report_id=%s", (rid,)).fetchone()
    assert row["signs_posted"] == "Danger" and row["bloom_textures"] == ["Benthic mats"]
    assert row["has_pictures"] is False and row["management_comments"] == "closed out"
