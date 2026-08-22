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


def test_triage_mode_blank_vs_identified(conn):
    """The mode facet splits totally-blank artifacts from ones carrying a station/date/id."""
    blank = _sample(conn, results=[{}])                             # no identity at all
    ident = _sample(conn, results=[{}])
    conn.execute("UPDATE sample SET sample_date='2026-06-15' WHERE id=%s", (ident,)); conn.commit()

    ids_blank = {r["id"] for r in empty_lab_records(conn, {"mode": "blank"})}
    ids_ident = {r["id"] for r in empty_lab_records(conn, {"mode": "identified"})}
    assert blank in ids_blank and ident not in ids_blank
    assert ident in ids_ident and blank not in ids_ident
    assert {blank, ident} <= {r["id"] for r in empty_lab_records(conn, {})}   # both under "all"


def test_analyte_filter_and_list(conn):
    """Empty rows still name an analyte; you can list them and filter by one."""
    from fhab.cleanup import empty_record_analytes
    aid = conn.execute("INSERT INTO analyte (analyte) VALUES ('Microcystins') RETURNING id").fetchone()["id"]
    with_an = _sample(conn, results=[{"analyte_id": aid}])          # blank result, but names an analyte
    without = _sample(conn, results=[{}])
    assert "Microcystins" in empty_record_analytes(conn)           # shown in the "used" list
    hit = {r["id"] for r in empty_lab_records(conn, {"analyte": "microcyst"})}   # partial, case-insensitive
    assert with_an in hit and without not in hit


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
