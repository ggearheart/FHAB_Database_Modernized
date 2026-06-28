"""Tests for the public submission queue: validation, the API endpoint, and staff promotion."""

import pytest

from fhab.auth import create_user, grant_role, set_password
from fhab.intake import SubmissionError, promote_submission, reject_submission, submit_public_report

R5 = "Region 5 - Central Valley"


def _payload(**over):
    base = dict(water_body_name="Phone Lake", county="Lake", latitude=39.0, longitude=-122.8,
                observation_date="2026-06-20", bloom_size="smaller than a sedan",
                bloom_textures=["Surface scum"], description="Green scum near the dock",
                reporter_name="Jo Public", reporter_email="jo@example.com")
    base.update(over)
    return base


def test_submit_validates_and_inserts_pending(conn):
    sid = submit_public_report(conn, _payload(), source="cyanosafe-demo", remote_ip="1.2.3.4")
    row = conn.execute("SELECT * FROM public_report_submission WHERE id=%s", (sid,)).fetchone()
    assert row["status"] == "pending" and row["water_body_name"] == "Phone Lake"
    assert row["bloom_textures"] == ["Surface scum"] and row["source"] == "cyanosafe-demo"


def test_submit_rejects_missing_name_and_out_of_state(conn):
    with pytest.raises(SubmissionError):
        submit_public_report(conn, _payload(water_body_name=""))
    with pytest.raises(SubmissionError):
        submit_public_report(conn, _payload(latitude=45.0, longitude=-100.0))  # outside CA
    with pytest.raises(SubmissionError):
        submit_public_report(conn, _payload(observation_date="2999-01-01"))  # future


def test_promote_creates_public_report_and_marks_promoted(conn):
    staff = create_user(conn, "rev@wb.ca.gov")
    grant_role(conn, staff, "wb_staff", region=R5)
    sid = submit_public_report(conn, _payload())
    brid = promote_submission(conn, staff, sid, region=R5)
    ev = conn.execute(
        """SELECT e.report_type, e.bloom_size, e.reporter_name, w.water_body_name
           FROM event e JOIN location l ON l.id=e.location_id JOIN waterbody w ON w.id=l.waterbody_id
           WHERE e.bloom_report_id=%s""", (brid,)).fetchone()
    assert ev["report_type"] == "Public Reporting" and ev["water_body_name"] == "Phone Lake"
    assert ev["reporter_name"] == "Jo Public"
    sub = conn.execute("SELECT status, promoted_report_id FROM public_report_submission WHERE id=%s",
                       (sid,)).fetchone()
    assert sub["status"] == "promoted" and sub["promoted_report_id"] == brid


def test_reject_marks_rejected(conn):
    staff = create_user(conn, "rev2@wb.ca.gov")
    grant_role(conn, staff, "wb_staff", region=R5)
    sid = submit_public_report(conn, _payload())
    reject_submission(conn, staff, sid, note="duplicate")
    assert conn.execute("SELECT status FROM public_report_submission WHERE id=%s",
                        (sid,)).fetchone()["status"] == "rejected"


# --- web endpoint tests ---

@pytest.fixture()
def client(conn):
    from tests.conftest import TEST_DSN
    from fhab.web import create_app
    staff = create_user(conn, "staff@wb.ca.gov", "Staffer")
    set_password(conn, staff, "pw"); grant_role(conn, staff, "wb_staff", region=R5)
    app = create_app(dsn=TEST_DSN)
    app.config["TESTING"] = True
    return app.test_client()


def test_public_endpoint_accepts_json_without_login(client, conn):
    r = client.post("/api/public/reports", json=_payload(source="cyanosafe-demo"))
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert conn.execute("SELECT count(*) c FROM public_report_submission").fetchone()["c"] == 1


def test_public_endpoint_cors_preflight(client):
    r = client.open("/api/public/reports", method="OPTIONS",
                    headers={"Origin": "https://ggearheart.github.io"})
    assert r.status_code == 204
    assert r.headers["Access-Control-Allow-Origin"] == "https://ggearheart.github.io"


def test_public_endpoint_honeypot_discards(client, conn):
    r = client.post("/api/public/reports", json=_payload(website="http://spam"))
    assert r.status_code == 200 and r.get_json()["id"] is None
    assert conn.execute("SELECT count(*) c FROM public_report_submission").fetchone()["c"] == 0


def test_public_endpoint_rejects_bad_payload(client):
    r = client.post("/api/public/reports", json={"description": "no name or location"})
    assert r.status_code == 400 and r.get_json()["ok"] is False


def test_review_queue_and_promote_via_web(client, conn):
    submit_public_report(conn, _payload())
    client.post("/login", data={"email": "staff@wb.ca.gov", "password": "pw"}, follow_redirects=True)
    r = client.get("/intake/review")
    assert b"Phone Lake" in r.data and b"Promote" in r.data
    sid = conn.execute("SELECT id FROM public_report_submission").fetchone()["id"]
    r = client.post(f"/intake/{sid}/promote", data={"region": R5}, follow_redirects=True)
    assert b"Public Reporting" in r.data  # landed on the new report's detail page
