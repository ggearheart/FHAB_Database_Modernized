"""Data cleanup: find empty lab-record artifacts and delete them safely."""

from fhab.cleanup import count_empty_records, delete_samples, empty_lab_records


def _sample(conn, *, results):
    """Create a sample with the given result rows (each a dict of result columns)."""
    sid = conn.execute("INSERT INTO sample DEFAULT VALUES RETURNING id").fetchone()["id"]
    for i, r in enumerate(results):
        cols = {"result_id_unique": f"{sid}-{i}", "sample_id": sid, "data_type": "Laboratory", **r}
        keys = ",".join(cols); ph = ",".join(["%s"] * len(cols))
        conn.execute(f"INSERT INTO result ({keys}) VALUES ({ph})", tuple(cols.values()))
    conn.commit()
    return sid


def test_empty_detection(conn):
    empty = _sample(conn, results=[{}])                              # a blank result row
    empty_no_results = conn.execute(
        "INSERT INTO sample DEFAULT VALUES RETURNING id").fetchone()["id"]; conn.commit()
    valued = _sample(conn, results=[{"measurement_value": 4.1}])     # has a value
    nd = _sample(conn, results=[{"res_qual_code": "ND"}])            # non-detect = real data
    txt = _sample(conn, results=[{"measurement_text": "present"}])   # has text

    found = {r["id"] for r in empty_lab_records(conn)}
    assert empty in found and empty_no_results in found             # empties listed
    assert valued not in found and nd not in found and txt not in found   # substantive excluded
    assert count_empty_records(conn) >= 2


def test_delete_only_removes_empty_unlinked(conn):
    empty = _sample(conn, results=[{}])
    valued = _sample(conn, results=[{"measurement_value": 1.0}])
    linked_empty = _sample(conn, results=[{}])
    conn.execute("INSERT INTO event (bloom_report_id) VALUES (777)")
    conn.execute("UPDATE sample SET bloom_report_id=777 WHERE id=%s", (linked_empty,)); conn.commit()

    # submit all three; only the empty *unlinked* one may be deleted
    res = delete_samples(conn, user_id=1, sample_ids=[empty, valued, linked_empty])
    assert res["deleted"] == 1 and res["skipped"] == 2
    assert conn.execute("SELECT count(*) c FROM sample WHERE id=%s", (empty,)).fetchone()["c"] == 0
    assert conn.execute("SELECT count(*) c FROM sample WHERE id=%s", (valued,)).fetchone()["c"] == 1   # safe
    assert conn.execute("SELECT count(*) c FROM sample WHERE id=%s", (linked_empty,)).fetchone()["c"] == 1
    # its result rows are gone too
    assert conn.execute("SELECT count(*) c FROM result WHERE sample_id=%s", (empty,)).fetchone()["c"] == 0


def test_delete_is_audited(conn):
    from fhab.auth import create_user
    uid = create_user(conn, "cleaner@wb.ca.gov")
    empty = _sample(conn, results=[{}])
    delete_samples(conn, user_id=uid, sample_ids=[empty])
    a = conn.execute("SELECT actor_id, action FROM audit_log WHERE table_name='sample' "
                     "AND row_key=%s", (str(empty),)).fetchone()
    assert a and a["action"] == "DELETE" and a["actor_id"] == uid


def test_include_linked_allows_deleting_a_linked_empty(conn):
    empty = _sample(conn, results=[{}])
    conn.execute("INSERT INTO event (bloom_report_id) VALUES (778)")
    conn.execute("UPDATE sample SET bloom_report_id=778 WHERE id=%s", (empty,)); conn.commit()
    assert delete_samples(conn, user_id=1, sample_ids=[empty])["deleted"] == 0       # guarded by default
    assert delete_samples(conn, user_id=1, sample_ids=[empty], include_linked=True)["deleted"] == 1
