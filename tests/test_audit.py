"""Row-level audit: triggers capture UPDATE/DELETE with actor, before/after, changed cols."""

from fhab.audit import count, recent


def _actor(conn, uid):
    conn.execute("SELECT set_config('fhab.user_id', %s, false)", (str(uid),))


def _event(conn, brid=1000000042):
    conn.execute("INSERT INTO event (bloom_report_id) VALUES (%s)", (brid,))
    conn.commit()
    return brid


def test_update_is_audited_with_actor_and_diff(conn):
    from fhab.auth import create_user
    uid = create_user(conn, "editor@wb.ca.gov")
    brid = _event(conn)
    _actor(conn, uid)
    conn.execute("UPDATE event SET determination_code = 'confirmed_hab' WHERE bloom_report_id = %s",
                 (brid,))
    conn.commit()

    rows = recent(conn, table="event", row_key=str(brid))
    assert len(rows) == 1
    r = rows[0]
    assert r["action"] == "UPDATE" and r["actor_id"] == uid
    assert "determination_code" in r["changed"]
    d = next(x for x in r["diff"] if x["col"] == "determination_code")
    assert d["before"] is None and d["after"] == "confirmed_hab"


def test_noop_update_not_audited(conn):
    brid = _event(conn)
    conn.execute("UPDATE event SET bloom_report_id = bloom_report_id WHERE bloom_report_id = %s",
                 (brid,))
    conn.commit()
    assert count(conn, table="event", row_key=str(brid)) == 0


def test_delete_is_audited(conn):
    # a sample delete (e.g. dedup merge) is captured with the prior row in `before`
    st = conn.execute("INSERT INTO station (station_code) VALUES ('SD1') RETURNING id").fetchone()["id"]
    sid = conn.execute("INSERT INTO sample (station_id, bg_id) VALUES (%s,'BGX') RETURNING id",
                       (st,)).fetchone()["id"]
    conn.commit()
    conn.execute("DELETE FROM sample WHERE id = %s", (sid,))
    conn.commit()

    rows = recent(conn, table="sample", row_key=str(sid))
    assert len(rows) == 1 and rows[0]["action"] == "DELETE"
    assert rows[0]["before"]["bg_id"] == "BGX" and rows[0]["after"] is None


def test_system_write_has_null_actor(conn):
    """No fhab.user_id set (loader / import / refresh) -> actor is NULL, still logged."""
    brid = _event(conn)
    conn.execute("SELECT set_config('fhab.user_id', '', false)")   # anonymous/system
    conn.execute("UPDATE event SET determination_code = 'no_bloom' WHERE bloom_report_id = %s",
                 (brid,))
    conn.commit()
    r = recent(conn, table="event", row_key=str(brid))[0]
    assert r["actor_id"] is None and "determination_code" in r["changed"]
