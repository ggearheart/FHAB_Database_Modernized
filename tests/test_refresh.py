"""Refresh from data.ca.gov: upsert existing + insert new, preserve local fields, dry-run rollback.

Uses the fixture flat files (no network); the CKAN download step is exercised separately/manually.
"""

import pytest

from fhab.refresh import refresh_from_dir
from tests.conftest import FIXTURES


def _an_event(conn):
    return conn.execute(
        "SELECT bloom_report_id, bloom_size FROM event ORDER BY bloom_report_id LIMIT 1").fetchone()


def test_dry_run_counts_but_rolls_back(loaded_conn):
    conn = loaded_conn
    eid = _an_event(conn)["bloom_report_id"]
    conn.execute("UPDATE event SET bloom_size='MUTATED' WHERE bloom_report_id=%s", (eid,))
    conn.commit()
    rep = refresh_from_dir(conn, FIXTURES, dry_run=True)
    assert rep.updated.get("events", 0) >= 1          # would update existing events
    # rolled back -> our mutation is still there, nothing was written
    assert conn.execute("SELECT bloom_size FROM event WHERE bloom_report_id=%s",
                        (eid,)).fetchone()["bloom_size"] == "MUTATED"


def test_apply_refreshes_published_but_preserves_local(loaded_conn):
    conn = loaded_conn
    row = _an_event(conn)
    eid, orig_size = row["bloom_report_id"], row["bloom_size"]
    conn.execute("UPDATE event SET bloom_size='MUTATED', owner_org='LOCAL_KEEP' "
                 "WHERE bloom_report_id=%s", (eid,))
    conn.commit()
    rep = refresh_from_dir(conn, FIXTURES, dry_run=False)
    r = conn.execute("SELECT bloom_size, owner_org FROM event WHERE bloom_report_id=%s",
                     (eid,)).fetchone()
    assert r["bloom_size"] == orig_size            # published field refreshed to authoritative value
    assert r["owner_org"] == "LOCAL_KEEP"           # local-only field untouched
    assert sum(rep.updated.values()) >= 1


def test_apply_inserts_missing_records(loaded_conn):
    conn = loaded_conn
    eid = conn.execute("SELECT bloom_report_id FROM event ORDER BY bloom_report_id DESC "
                       "LIMIT 1").fetchone()["bloom_report_id"]
    conn.execute("DELETE FROM advisory WHERE response_action_id IN "
                 "(SELECT response_action_id FROM response WHERE bloom_report_id=%s)", (eid,))
    conn.execute("DELETE FROM response WHERE bloom_report_id=%s", (eid,))
    conn.execute("DELETE FROM result WHERE sample_id IN (SELECT id FROM sample WHERE bloom_report_id=%s)", (eid,))
    conn.execute("DELETE FROM sample WHERE bloom_report_id=%s", (eid,))
    conn.execute("DELETE FROM event WHERE bloom_report_id=%s", (eid,))
    conn.commit()
    rep = refresh_from_dir(conn, FIXTURES, dry_run=False)
    assert rep.inserted.get("events", 0) >= 1
    assert conn.execute("SELECT 1 FROM event WHERE bloom_report_id=%s", (eid,)).fetchone()


def test_no_deletes(loaded_conn):
    conn = loaded_conn
    # A locally-created event with an id not in the published files must survive a refresh.
    conn.execute("INSERT INTO event (bloom_report_id, report_type) VALUES (999999, 'Local only')")
    conn.commit()
    refresh_from_dir(conn, FIXTURES, dry_run=False)
    assert conn.execute("SELECT 1 FROM event WHERE bloom_report_id=999999").fetchone()


# --- web ---

@pytest.fixture()
def client(conn):
    from fhab.auth import create_user, grant_role, set_password
    from fhab.web import create_app
    from tests.conftest import TEST_DSN
    admin = create_user(conn, "admin@wb.ca.gov"); set_password(conn, admin, "pw")
    grant_role(conn, admin, "program_admin")
    staff = create_user(conn, "st@wb.ca.gov"); set_password(conn, staff, "pw")
    grant_role(conn, staff, "wb_staff", region="Region 5")
    app = create_app(dsn=TEST_DSN); app.config["TESTING"] = True
    return app.test_client()


def test_refresh_page_admin_only(client, conn):
    client.post("/login", data={"email": "st@wb.ca.gov", "password": "pw"}, follow_redirects=True)
    assert b"Administrator access required" in client.get("/admin/refresh", follow_redirects=True).data
    client.get("/logout")
    client.post("/login", data={"email": "admin@wb.ca.gov", "password": "pw"}, follow_redirects=True)
    r = client.get("/admin/refresh")
    assert r.status_code == 200 and b"Refresh from data.ca.gov" in r.data
