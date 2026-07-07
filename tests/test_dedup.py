"""Governance #1: detect + merge duplicate samples across ingest paths."""

import pytest

from fhab.auth import create_user, grant_role, set_password
from fhab.dedup import candidate_duplicate_samples, duplicate_count, merge_samples

R5 = "Region 5 - Central Valley"


def _dup_pair(conn, *, code="533DUP", date="2025-06-02", lab_id="L1"):
    """Two samples with the same station+date+type (a duplicate candidate), each with a result."""
    st = conn.execute("INSERT INTO station (station_code) VALUES (%s) ON CONFLICT (station_code) "
                      "DO UPDATE SET station_code=EXCLUDED.station_code RETURNING id", (code,)).fetchone()["id"]
    ids = []
    for i in range(2):
        sid = conn.execute("INSERT INTO sample (station_id, sample_date, sample_type, lab_sample_id) "
                           "VALUES (%s,%s,'Water Grab',%s) RETURNING id", (st, date, lab_id)).fetchone()["id"]
        an = conn.execute("SELECT id FROM analyte LIMIT 1").fetchone()["id"]
        conn.execute("INSERT INTO result (result_id_unique, sample_id, analyte_id, method, measurement_value) "
                     "VALUES (%s,%s,%s,'ELISA',%s)", (f"{code}-{i}", sid, an, 1.0))
        ids.append(sid)
    conn.commit()
    return ids


def test_detects_and_merges_duplicates(conn):
    a, b = _dup_pair(conn)
    groups = candidate_duplicate_samples(conn)
    grp = next(g for g in groups if {m["id"] for m in g["members"]} == {a, b})
    assert grp["n"] == 2 and duplicate_count(conn) >= 1

    res = merge_samples(conn, None, a, [a, b])
    assert res["merged"] == 1
    # b is gone; its result was repointed onto a, then de-duped (same analyte/method) -> 1 result on a
    assert conn.execute("SELECT 1 FROM sample WHERE id=%s", (b,)).fetchone() is None
    assert conn.execute("SELECT count(*) c FROM result WHERE sample_id=%s", (a,)).fetchone()["c"] == 1
    assert not any({m["id"] for m in g["members"]} == {a, b} for g in candidate_duplicate_samples(conn))


def test_merge_repoints_links(conn):
    a, b = _dup_pair(conn, code="533LNK", lab_id="L2")
    # b carries a CEDEN station link and a sample_link row that must survive on a.
    conn.execute("INSERT INTO sample_station_link (sample_id, station_code) VALUES (%s,'CEDX')", (b,))
    conn.execute("INSERT INTO sample_link (sample_id, match_method) VALUES (%s,'manual')", (b,))
    conn.commit()
    merge_samples(conn, None, a, [a, b])
    assert conn.execute("SELECT 1 FROM sample_station_link WHERE sample_id=%s AND station_code='CEDX'", (a,)).fetchone()
    assert conn.execute("SELECT 1 FROM sample_link WHERE sample_id=%s", (a,)).fetchone()


def test_merge_excludes_survivor_and_unchecked(conn):
    a, b = _dup_pair(conn, code="533EXC", lab_id="L3")
    # only 'a' checked (b left out) -> nothing to merge
    assert merge_samples(conn, None, a, [a])["merged"] == 0
    assert conn.execute("SELECT 1 FROM sample WHERE id=%s", (b,)).fetchone()   # b untouched


# --- web ---

@pytest.fixture()
def client(conn):
    from fhab.web import create_app
    from tests.conftest import TEST_DSN
    staff = create_user(conn, "staff@wb.ca.gov"); set_password(conn, staff, "pw")
    grant_role(conn, staff, "wb_staff", region=R5)
    app = create_app(dsn=TEST_DSN); app.config["TESTING"] = True
    return app.test_client()


def test_dedup_web(client, conn):
    a, b = _dup_pair(conn, code="533WEB", lab_id="L4")
    client.post("/login", data={"email": "staff@wb.ca.gov", "password": "pw"}, follow_redirects=True)
    page = client.get("/lab/duplicates")
    assert page.status_code == 200 and b"533WEB" in page.data
    client.post("/lab/duplicates", data={"survivor": str(a), "member": [str(a), str(b)]},
                follow_redirects=True)
    assert conn.execute("SELECT 1 FROM sample WHERE id=%s", (b,)).fetchone() is None
