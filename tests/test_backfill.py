"""Tests for the determination backfill from advisory signals."""

from fhab.backfill import backfill_determination


def _report(conn, brid, advisory_recommended=None, advisory_detail=None, determination=None):
    """Insert an event + a response + advisory carrying the given signals."""
    wb = conn.execute(
        "INSERT INTO waterbody (water_body_name) VALUES (%s) RETURNING id", (f"WB{brid}",)
    ).fetchone()["id"]
    loc = conn.execute("INSERT INTO location (waterbody_id) VALUES (%s) RETURNING id", (wb,)).fetchone()["id"]
    conn.execute(
        "INSERT INTO event (bloom_report_id, location_id, determination_code) VALUES (%s,%s,%s)",
        (brid, loc, determination))
    conn.execute("INSERT INTO response (response_action_id, bloom_report_id) VALUES (%s,%s)", (brid, brid))
    conn.execute(
        """INSERT INTO advisory (advisory_id, response_action_id, advisory_recommended, advisory_detail)
           VALUES (%s,%s,%s,%s)""", (brid, brid, advisory_recommended, advisory_detail))
    conn.commit()


def _det(conn, brid):
    return conn.execute(
        "SELECT determination_code FROM event WHERE bloom_report_id=%s", (brid,)).fetchone()["determination_code"]


def test_backfill_maps_signals_to_determinations(conn):
    _report(conn, 1, advisory_recommended="Danger", advisory_detail="Advisory based on visual and toxins")
    _report(conn, 2, advisory_detail="Marine bloom present (red tide)")
    _report(conn, 3, advisory_detail="No cyano, other algae present.")
    _report(conn, 4, advisory_detail="Potential spill")
    _report(conn, 5, advisory_detail="Confirmed no bloom")
    _report(conn, 6, advisory_recommended="Caution", advisory_detail="Updates from routine monitoring")
    _report(conn, 7, advisory_recommended="None", advisory_detail="Under investigation")  # stays null

    backfill_determination(conn)

    assert _det(conn, 1) == "confirmed_hab"
    assert _det(conn, 2) == "red_tide"
    assert _det(conn, 3) == "non_hab_algae"
    assert _det(conn, 4) == "spill"
    assert _det(conn, 5) == "no_bloom"
    assert _det(conn, 6) == "confirmed_hab"
    assert _det(conn, 7) is None


def test_backfill_does_not_override_existing(conn):
    _report(conn, 10, advisory_recommended="Danger", determination="non_hab_algae")
    backfill_determination(conn)
    assert _det(conn, 10) == "non_hab_algae"  # staff value preserved
