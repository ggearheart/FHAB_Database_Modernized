"""DB ops: session-timeout GUCs are applied, and stuck-session helpers behave."""

import os

import pytest

from fhab.dbops import activity_summary, clear_stuck, session_activity


def test_connect_sets_session_timeouts():
    """A connection opened via fhab.db.connect carries the self-heal / lock-timeout GUCs."""
    from fhab.db import connect
    try:
        c = connect(os.environ.get("FHAB_TEST_DATABASE_URL", "dbname=fhab_test host=/tmp port=5432"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres not available ({exc})")
    try:
        assert c.execute("SHOW idle_in_transaction_session_timeout").fetchone()[
            "idle_in_transaction_session_timeout"] == "2min"
        assert c.execute("SHOW lock_timeout").fetchone()["lock_timeout"] == "30s"
    finally:
        c.close()


def test_session_activity_and_clear_noop(conn):
    """With no stuck sessions, activity lists cleanly and clear_stuck is a safe no-op."""
    rows = session_activity(conn)
    assert isinstance(rows, list)
    summ = activity_summary(conn)
    assert summ["idle_in_transaction"] == 0 and summ["blocked"] == 0
    assert clear_stuck(conn) == []          # nothing idle-in-transaction to terminate


def test_clear_stuck_terminates_idle_in_transaction(conn):
    """Open a second connection, leave it idle-in-transaction, and confirm clear_stuck kills it."""
    import psycopg
    from tests.conftest import TEST_DSN
    zombie = psycopg.connect(TEST_DSN, row_factory=psycopg.rows.dict_row)
    zombie.execute("SELECT 1")              # begins a transaction, now idle-in-transaction
    zpid = zombie.execute("SELECT pg_backend_pid() AS p").fetchone()["p"]
    # visible as idle-in-transaction from the primary connection
    states = {r["pid"]: r["state"] for r in session_activity(conn)}
    assert states.get(zpid) == "idle in transaction"

    killed = clear_stuck(conn, older_than_secs=0)   # 0s threshold -> eligible immediately
    assert zpid in killed
    try:
        zombie.close()
    except Exception:  # noqa: BLE001
        pass
