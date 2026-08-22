"""Governance #4: the web app connects as the table owner, which *bypasses* Row-Level Security.
Running requests under the non-owning `fhab_web` role (via `web_user`) makes the policies an actual
database backstop. These tests prove the difference: the same query that the owner path returns in
full is filtered — or denied — once it runs under `web_user`, even with no app-layer WHERE clause.
"""

from fhab.auth import create_user, grant_role, web_user

R5, R1 = "Region 5", "Region 1"


def _region_event(conn, brid, region):
    """A minimal event in `region`: waterbody -> location -> event."""
    wb = conn.execute("INSERT INTO waterbody (water_body_name, regional_water_board) "
                      "VALUES (%s,%s) RETURNING id", (f"WB{brid}", region)).fetchone()["id"]
    loc = conn.execute("INSERT INTO location (waterbody_id) VALUES (%s) RETURNING id", (wb,)).fetchone()["id"]
    conn.execute("INSERT INTO event (bloom_report_id, location_id, observation_date) "
                 "VALUES (%s,%s,'2026-06-01')", (brid, loc))
    conn.commit()


def test_web_user_enforces_region_scoping_owner_bypasses(conn):
    """A region-scoped staffer, querying `event` with NO region filter, sees only their region
    under web_user — the RLS policy does the scoping the query omits. The owner sees both."""
    _region_event(conn, 6001, R5)
    _region_event(conn, 6002, R1)
    staff = create_user(conn, "r5@wb.ca.gov"); grant_role(conn, staff, "wb_staff", region=R5)

    # Owner path (RLS bypassed): both regions visible.
    owner_ids = {r["bloom_report_id"] for r in
                 conn.execute("SELECT bloom_report_id FROM event").fetchall()}
    assert {6001, 6002} <= owner_ids

    # web_user path (RLS enforced): only the staffer's own region, despite the same unfiltered query.
    with web_user(conn, staff):
        seen = {r["bloom_report_id"] for r in
                conn.execute("SELECT bloom_report_id FROM event").fetchall()}
    assert 6001 in seen and 6002 not in seen


def test_web_user_locks_down_reporter_pii(conn):
    """The reporter-PII table is internal-staff-only by policy. A non-internal user (here a
    community-science volunteer) reads zero rows under web_user, even though the owner path — and a
    forgotten guard — would expose name/email/phone/remote_ip."""
    conn.execute(
        "INSERT INTO public_report_submission (reporter_name, reporter_email, reporter_phone, "
        "remote_ip, status) VALUES ('Jo Doe','jo@example.org','555-0100','203.0.113.7','pending')")
    conn.commit()
    volunteer = create_user(conn, "vol@example.org")
    grant_role(conn, volunteer, "comm_sci_volunteer", org="Creek Watch")

    assert conn.execute("SELECT count(*) c FROM public_report_submission").fetchone()["c"] == 1  # owner
    with web_user(conn, volunteer):
        assert conn.execute("SELECT count(*) c FROM public_report_submission").fetchone()["c"] == 0


def test_web_user_blocks_cross_region_write(conn):
    """Case management is region-scoped: a Region 5 staffer cannot INSERT a case on a Region 1
    waterbody under web_user (the write policy's CHECK denies it at the DB)."""
    import psycopg
    wb1 = conn.execute("INSERT INTO waterbody (water_body_name, regional_water_board) "
                       "VALUES ('Far Lake',%s) RETURNING id", (R1,)).fetchone()["id"]
    staff = create_user(conn, "r5w@wb.ca.gov"); grant_role(conn, staff, "wb_staff", region=R5)
    denied = False
    with web_user(conn, staff):
        try:
            conn.execute("INSERT INTO hab_case (case_id, waterbody_id) VALUES (91001,%s)", (wb1,))
            conn.commit()
        except psycopg.errors.InsufficientPrivilege:
            denied = True
    assert denied
    assert conn.execute("SELECT count(*) c FROM hab_case WHERE case_id=91001").fetchone()["c"] == 0
