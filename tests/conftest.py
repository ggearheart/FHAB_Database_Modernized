import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))  # so `tests.conftest` is importable from test modules

FIXTURES = Path(__file__).parent / "fixtures" / "ca_fhab"

# DB tests use a dedicated test database; override with FHAB_TEST_DATABASE_URL.
TEST_DSN = os.environ.get(
    "FHAB_TEST_DATABASE_URL",
    os.environ.get("FHAB_DATABASE_URL", "dbname=fhab_test host=/tmp port=5432"),
)


@pytest.fixture()
def conn():
    """A connection to a freshly-reset test database. Skips if Postgres is unreachable."""
    psycopg = pytest.importorskip("psycopg")
    try:
        c = psycopg.connect(TEST_DSN, row_factory=psycopg.rows.dict_row)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres not available ({exc}); set FHAB_TEST_DATABASE_URL")
    from fhab.db import reset_schema

    try:
        reset_schema(c)
    except Exception as exc:  # e.g. PostGIS not installed
        c.close()
        pytest.skip(f"Could not reset schema (PostGIS missing?): {exc}")
    yield c
    c.close()


@pytest.fixture()
def loaded_conn(conn):
    """Test DB with the fixture flat files loaded."""
    from fhab.loaders import load_open_data

    load_open_data(conn, FIXTURES)
    return conn
