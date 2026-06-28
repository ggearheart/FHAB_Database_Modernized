"""Tests for full-form illness on the public endpoint + community-group API keys / trust lane."""

import pytest

from fhab.auth import create_user, grant_role, set_password
from fhab.intake import (SubmissionError, create_intake_group, list_submissions,
                         promote_submission, promote_trusted_pending, resolve_intake_group,
                         set_group_active, submit_public_report)

R5 = "Region 5 - Central Valley"


def _payload(**over):
    base = dict(water_body_name="Group Lake", county="Lake", latitude=39.0, longitude=-122.8,
                observation_date="2026-06-20", description="scum")
    base.update(over)
    return base


def _admin(conn):
    u = create_user(conn, "admin@wb.ca.gov"); grant_role(conn, u, "program_admin")
    return u


def _staff(conn, email="cm@wb.ca.gov"):
    u = create_user(conn, email); grant_role(conn, u, "wb_staff", region=R5)
    return u


def test_full_form_illness_is_accepted_and_carried_to_report(conn):
    staff = _staff(conn)
    sid = submit_public_report(conn, _payload(
        illness=[{"subject": "Dog", "illness": True, "death": True},
                 {"subject": "Fish", "illness": False, "death": False}],   # dropped
        illness_description="dog sick after swim"))
    row = conn.execute("SELECT illness, illness_description FROM public_report_submission WHERE id=%s",
                       (sid,)).fetchone()
    assert row["illness"] == [{"subject": "Dog", "illness": True, "death": True}]
    brid = promote_submission(conn, staff, sid, region=R5)
    ill = conn.execute("SELECT subject, illness, death FROM report_illness WHERE bloom_report_id=%s",
                       (brid,)).fetchone()
    assert ill["subject"] == "Dog" and ill["illness"] and ill["death"]


def test_group_key_attributes_and_tiers_submission(conn):
    admin = _admin(conn)
    gid, key = create_intake_group(conn, admin, "Clear Lake Volunteers",
                                   tier="community", trusted=True)
    g = resolve_intake_group(conn, key)
    assert g["id"] == gid and g["trusted"] is True
    # The route passes the group's attribution explicitly; the payload can't set these itself.
    from fhab.intake import TIER_REPORT_TYPE
    sid = submit_public_report(conn, _payload(), source=g["group_name"],
                               report_type=TIER_REPORT_TYPE[g["tier"]], group_id=gid, trusted=True)
    row = conn.execute("SELECT source, report_type, trusted, group_id FROM public_report_submission WHERE id=%s",
                       (sid,)).fetchone()
    assert row["source"] == "Clear Lake Volunteers" and row["report_type"] == "Agency/Partner Reporting"
    assert row["trusted"] is True and row["group_id"] == gid


def test_promote_uses_submitted_report_type(conn):
    staff = _staff(conn)
    sid = submit_public_report(conn, _payload(), report_type="Agency/Partner Reporting")
    brid = promote_submission(conn, staff, sid, region=R5)
    rt = conn.execute("SELECT report_type FROM event WHERE bloom_report_id=%s", (brid,)).fetchone()
    assert rt["report_type"] == "Agency/Partner Reporting"


def test_promote_trusted_pending_only_promotes_trusted(conn):
    staff = _staff(conn)
    s_trusted = submit_public_report(conn, _payload(water_body_name="Trust Lake"), trusted=True)
    s_public = submit_public_report(conn, _payload(water_body_name="Anon Lake"))
    n = promote_trusted_pending(conn, staff)
    assert n == 1
    assert conn.execute("SELECT status FROM public_report_submission WHERE id=%s",
                        (s_trusted,)).fetchone()["status"] == "promoted"
    assert conn.execute("SELECT status FROM public_report_submission WHERE id=%s",
                        (s_public,)).fetchone()["status"] == "pending"
    assert len(list_submissions(conn, staff, "pending", trusted_only=True)) == 0


def test_revoked_group_key_no_longer_resolves(conn):
    admin = _admin(conn)
    gid, key = create_intake_group(conn, admin, "Old Group")
    set_group_active(conn, admin, gid, False)
    assert resolve_intake_group(conn, key) is None


# --- web: endpoint attributes a keyed submission; payload cannot spoof a tier ---

@pytest.fixture()
def client(conn):
    from tests.conftest import TEST_DSN
    from fhab.web import create_app
    admin = create_user(conn, "admin@fhab.local", "Admin")
    set_password(conn, admin, "pw"); grant_role(conn, admin, "program_admin")
    app = create_app(dsn=TEST_DSN); app.config["TESTING"] = True
    return app.test_client()


def test_payload_cannot_spoof_partner_tier(client, conn):
    # No key: even if the payload claims a partner report_type, it's ignored -> stays public/untrusted.
    client.post("/api/public/reports", json=_payload(report_type="Agency/Partner Reporting",
                                                     trusted=True, group_id=999))
    row = conn.execute("SELECT report_type, trusted, group_id FROM public_report_submission").fetchone()
    assert row["report_type"] is None and row["trusted"] is False and row["group_id"] is None


def test_keyed_submission_is_attributed_via_endpoint(client, conn):
    admin = conn.execute("SELECT id FROM app_user WHERE email='admin@fhab.local'").fetchone()["id"]
    _, key = create_intake_group(conn, admin, "Partner Org", tier="agency", trusted=True)
    r = client.post("/api/public/reports", json=_payload(), headers={"X-API-Key": key})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    row = conn.execute("SELECT source, report_type, trusted FROM public_report_submission").fetchone()
    assert row["source"] == "Partner Org" and row["report_type"] == "Agency/Partner Reporting"
    assert row["trusted"] is True
