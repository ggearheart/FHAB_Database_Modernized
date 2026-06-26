"""Row-Level Security tests: role + scope determine which rows each user sees."""

import pytest

from fhab.auth import acting_as, create_user, grant_role

R5 = "Region 5 - Central Valley"
R1 = "Region 1 - North Coast"


@pytest.fixture()
def world(conn):
    """A small controlled dataset: two regions, published vs unpublished advisories."""
    def wb(name, region):
        return conn.execute(
            "INSERT INTO waterbody (water_body_name, regional_water_board) VALUES (%s,%s) RETURNING id",
            (name, region),
        ).fetchone()["id"]

    def loc(wbid):
        return conn.execute(
            "INSERT INTO location (waterbody_id) VALUES (%s) RETURNING id", (wbid,)
        ).fetchone()["id"]

    def event(brid, locid):
        conn.execute(
            "INSERT INTO event (bloom_report_id, location_id) VALUES (%s,%s)", (brid, locid)
        )

    def advisory(aid, brid, display):
        conn.execute(
            "INSERT INTO response (response_action_id, bloom_report_id) VALUES (%s,%s)", (aid, brid)
        )
        conn.execute(
            """INSERT INTO advisory (advisory_id, response_action_id, display_advisory_on_map)
               VALUES (%s,%s,%s)""",
            (aid, aid, display),
        )

    wb_a = wb("Lake A", R5)        # Region 5
    wb_b = wb("Lake B", R1)        # Region 1
    event(1, loc(wb_a)); advisory(1, 1, True)    # EA: Region 5, published
    event(2, loc(wb_b)); advisory(2, 2, False)   # EB: Region 1, NOT published
    event(3, loc(wb_a))                          # EC: Region 5, no advisory (private)
    conn.commit()
    return {"wb_a": wb_a, "wb_b": wb_b}


def _counts(conn, user_id):
    with acting_as(conn, user_id):
        return {
            "events": conn.execute("SELECT count(*) n FROM event").fetchone()["n"],
            "advisories": conn.execute("SELECT count(*) n FROM advisory").fetchone()["n"],
            "responses": conn.execute("SELECT count(*) n FROM response").fetchone()["n"],
        }


def test_role_catalog_seeded(conn):
    n = conn.execute("SELECT count(*) n FROM role").fetchone()["n"]
    assert n >= 14
    assert conn.execute("SELECT category FROM role WHERE code='wb_staff'").fetchone()["category"] == "internal_staff"


def test_admin_sees_everything(conn, world):
    admin = create_user(conn, "admin@wb.ca.gov")
    grant_role(conn, admin, "program_admin")
    c = _counts(conn, admin)
    assert c == {"events": 3, "advisories": 2, "responses": 2}


def test_regional_staff_scoped_to_region(conn, world):
    r5 = create_user(conn, "r5@wb.ca.gov"); grant_role(conn, r5, "wb_staff", region=R5)
    r1 = create_user(conn, "r1@wb.ca.gov"); grant_role(conn, r1, "wb_staff", region=R1)
    # Region 5 staff see EA + EC (their region), not EB.
    assert _counts(conn, r5)["events"] == 2
    # Region 1 staff see EB only.
    assert _counts(conn, r1)["events"] == 1


def test_public_sees_only_published(conn, world):
    pub = create_user(conn, "jane@public.org"); grant_role(conn, pub, "public")
    c = _counts(conn, pub)
    assert c["events"] == 1          # only EA (published)
    assert c["advisories"] == 1      # only the displayed advisory
    assert c["responses"] == 0       # internal-only table hidden


def test_anonymous_matches_public(conn, world):
    assert _counts(conn, None)["events"] == 1


def test_water_body_manager_sees_their_waterbody(conn, world):
    mgr = create_user(conn, "mgr@lakea.org")
    grant_role(conn, mgr, "water_body_manager", waterbody_id=world["wb_a"])
    # Sees Lake A's private event EC plus the published EA; not EB (different waterbody, unpublished).
    c = _counts(conn, mgr)
    assert c["events"] == 2
    bids = None
    with acting_as(conn, mgr):
        bids = {r["bloom_report_id"] for r in conn.execute(
            "SELECT bloom_report_id FROM event").fetchall()}
    assert bids == {1, 3}
