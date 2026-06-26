# FHAB Database Modernized

A modernized data pipeline and schema for **Freshwater Harmful Algal Bloom (FHAB)** monitoring data.

This project provides a clean, reproducible way to ingest, validate, and store FHAB
incident and monitoring records — replacing ad-hoc spreadsheets with a versioned
schema, automated ETL, and tested data-quality checks.

## Goals

- **CRM lifecycle** — a normalized model of the CA FHAB case-management lifecycle:
  `report → event → case`, with `response` (advisories) and `result` (field/lab analysis).
- **Backwards compatible** — field names and integer IDs mirror the published open-data
  reports; the four published flat files are regenerated as exports.
- **Geospatial** — PostgreSQL + PostGIS, with HUC-12 watershed linkage and Geoconnex
  persistent identifiers for HAB locations.
- **Reproducible & tested** — scripted load of the published data and a pytest suite.

## Project layout

```
.
├── src/fhab/          # Python package: db, parsing, loaders, export
├── sql/               # PostgreSQL + PostGIS schema
├── scripts/           # devdb.sh, init_db.py, fetch/export CLIs
├── data/raw/          # Source files (gitignored)
├── tests/             # Test suite (+ fixtures sampled from real data)
└── docs/              # Requirements, data model, proposals, reviews
```

## Quick start

Requires Homebrew. Sets up a local PostgreSQL 17 + PostGIS dev database.

```bash
brew install postgresql@17 postgis
pip install -e ".[dev]"               # installs psycopg

# Start the dev DB and create the `fhab` database (idempotent)
bash scripts/devdb.sh
export FHAB_DATABASE_URL="dbname=fhab host=/tmp port=5432"

# Pull the published CA FHAB reference data into data/raw/
python scripts/fetch_reference_data.py

# Apply schema, load the four published flat files, re-export them
python scripts/init_db.py --reset --load --export /tmp/fhab_export
```

Run the tests (creates/uses a `fhab_test` database):

```bash
createdb fhab_test && psql -d fhab_test -c "CREATE EXTENSION postgis;"
export FHAB_TEST_DATABASE_URL="dbname=fhab_test host=/tmp port=5432"
pytest -q
```

## Status

**Working build.** The PostgreSQL + PostGIS CRM schema is implemented; the four published
flat files load into the normalized model and re-export with matching row counts and
published headers. The lifecycle (`report → event → case`, `response`/`advisory`,
3-level analyte taxonomy incl. genetic `mcyE` / cyanotoxin) and PostGIS geometry are
exercised by the test suite (22 tests).

The **Bend→CEDEN ingestion** is also built: `fhab.ceden` loads a Bend_CEDEN_workflow output
pair (FieldResults + WaterChemistry) into `station`/`sample`/`result`, **filling the analyte
values that are blank in the FHAB data**, and links samples to FHAB events/cases where they
coincide. Load with `init_db.py --ceden FIELD_CSV CHEMISTRY_CSV`.

**Next:** enrich `station.geom` from a CEDEN/SWAMP station registry (enables spatial
linking), load the HUC-12 watershed layer + point-in-polygon derivation (`GEO-4`),
Geoconnex PID minting (`GEO-1`), full-fidelity export of all published columns, and the
external Tier 1–3 ingestion path.

- [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) — business requirements: the external
  three-tier ingestion model (IoW / CA State Water Boards Phase 1) **and** the internal
  CRM case-management lifecycle.
- [docs/DATA_MODEL_CA_FHAB.md](docs/DATA_MODEL_CA_FHAB.md) — the authoritative target:
  the published CA FHAB model (Report → Case → Response → Result + Advisory) and its
  four flat files on the California Open Data Portal.
- [docs/SCHEMA_PROPOSAL.md](docs/SCHEMA_PROPOSAL.md) — the PostgreSQL + PostGIS design,
  now implemented in [sql/schema.sql](sql/schema.sql).
- [docs/GEOCONNEX.md](docs/GEOCONNEX.md) — persistent URL identifiers for HAB locations
  via Geoconnex, and HUC-12 watershed linkage.
- [docs/BEND_CEDEN_WORKFLOW.md](docs/BEND_CEDEN_WORKFLOW.md) — how the
  [Bend_CEDEN_workflow](https://github.com/ggearheart/Bend_CEDEN_workflow) tool (Bend→CEDEN
  2.0 conversion) and the FHAB DB connect: ingesting the tool's CEDEN output to fill the
  missing analyte values and linking stations/samples to FHAB events/cases.
- [docs/LEGACY_SCHEMA_REVIEW.md](docs/LEGACY_SCHEMA_REVIEW.md) — analysis of the existing
  (problematic) database that produces the open data: what it validates, what to adopt,
  and the anti-patterns to fix.
- [docs/GAP_ANALYSIS.md](docs/GAP_ANALYSIS.md) — how the scaffold mapped to the
  requirements (historical; superseded by the implemented schema).

## License

MIT — see [LICENSE](LICENSE).
