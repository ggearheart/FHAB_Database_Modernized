"""Tests for controlled-vocabulary place helpers (county list + fuzzy waterbody matching)."""

from fhab.places import COUNTIES, similar_waterbodies, suggest_waterbodies


def _seed(conn):
    for n in ["Clear Lake", "Clear Creek", "Pyramid Lake", "Lake Elsinore"]:
        conn.execute("INSERT INTO waterbody (water_body_name, county) VALUES (%s, %s)", (n, "Lake"))
    conn.commit()


def test_county_list_is_closed_and_complete(conn):
    assert "Sacramento" in COUNTIES and "Arizona State" in COUNTIES
    assert len(COUNTIES) == 61  # 58 CA counties + 3 out-of-state


def test_suggest_prefix_then_fuzzy(conn):
    _seed(conn)
    names = [r["name"] for r in suggest_waterbodies(conn, "clear")]
    assert "Clear Lake" in names and "Clear Creek" in names
    # A typo (no space) still finds Clear Lake via trigram similarity.
    assert "Clear Lake" in [r["name"] for r in suggest_waterbodies(conn, "clearlake")]


def test_similar_catches_near_dup_excludes_exact(conn):
    _seed(conn)
    sims = similar_waterbodies(conn, "Clearlake")
    assert any(s["name"] == "Clear Lake" for s in sims)
    # An exact (canonical) name has no near-duplicate suggestions of itself.
    assert all(s["name"].lower() != "clear lake" for s in similar_waterbodies(conn, "Clear Lake"))
