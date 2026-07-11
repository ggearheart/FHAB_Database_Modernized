"""Admin notification settings: email-forward toggle wired into new-report notifications."""

import pytest

from fhab import notify
from fhab.auth import create_user, grant_role, set_password
from fhab.settings import EMAIL_NEW_REPORT, FORWARD_TO, email_new_report_enabled, get_setting, set_setting


def test_get_set_setting(conn):
    assert get_setting(conn, EMAIL_NEW_REPORT, "0") == "0"
    set_setting(conn, EMAIL_NEW_REPORT, "1")
    assert get_setting(conn, EMAIL_NEW_REPORT) == "1" and email_new_report_enabled(conn)


def test_new_submission_emails_only_when_enabled(conn, monkeypatch):
    rev = create_user(conn, "rev@wb.ca.gov"); grant_role(conn, rev, "wb_staff")
    sub = conn.execute("INSERT INTO public_report_submission (water_body_name) VALUES ('Lake') "
                       "RETURNING id").fetchone()["id"]; conn.commit()
    sent = []
    monkeypatch.setattr(notify, "send_email", lambda to, subj, body: sent.append(to) or True)

    notify.on_new_submission(conn, sub, water_body="Lake")           # disabled -> no email
    assert sent == []

    set_setting(conn, EMAIL_NEW_REPORT, "1")
    set_setting(conn, FORWARD_TO, "inbox@example.gov")
    notify.on_new_submission(conn, sub, water_body="Lake")           # enabled -> reviewer + forward
    assert "inbox@example.gov" in sent and "rev@wb.ca.gov" in sent


# --- web ---

@pytest.fixture()
def client(conn):
    from fhab.web import create_app
    from tests.conftest import TEST_DSN
    admin = create_user(conn, "admin@wb.ca.gov"); set_password(conn, admin, "pw")
    grant_role(conn, admin, "program_admin")
    staff = create_user(conn, "st@wb.ca.gov"); set_password(conn, staff, "pw")
    grant_role(conn, staff, "wb_staff", region="Region 5")
    app = create_app(dsn=TEST_DSN); app.config["TESTING"] = True
    return app.test_client()


def test_admin_notifications_web(client, conn):
    client.post("/login", data={"email": "admin@wb.ca.gov", "password": "pw"}, follow_redirects=True)
    client.post("/admin/notifications", data={"email_on": "1", "forward_to": "inbox@example.gov"},
                follow_redirects=True)
    assert get_setting(conn, EMAIL_NEW_REPORT) == "1"
    assert get_setting(conn, FORWARD_TO) == "inbox@example.gov"
    # unchecking the box disables it
    client.post("/admin/notifications", data={"forward_to": "inbox@example.gov"}, follow_redirects=True)
    assert get_setting(conn, EMAIL_NEW_REPORT) == "0"


def test_notifications_admin_only(client, conn):
    client.post("/login", data={"email": "st@wb.ca.gov", "password": "pw"}, follow_redirects=True)
    assert b"Administrator access required" in client.get("/admin/notifications", follow_redirects=True).data
