# FHAB Database Modernized

A modernized data pipeline and schema for **Freshwater Harmful Algal Bloom (FHAB)** monitoring data.

This project provides a clean, reproducible way to ingest, validate, and store FHAB
incident and monitoring records — replacing ad-hoc spreadsheets with a versioned
schema, automated ETL, and tested data-quality checks.

## Goals

- **Modern schema** — a normalized relational model for waterbodies, monitoring sites, samples, and bloom advisories.
- **Reproducible ETL** — scripted ingestion from raw sources (CSV/Excel/API) into a clean database.
- **Data quality** — validation rules and tests so bad records are caught before they land.
- **Open & portable** — defaults to SQLite for zero-setup local use; portable to PostgreSQL.

## Project layout

```
.
├── src/fhab/          # Python package: ETL, models, validation
├── sql/               # Schema definitions and migrations
├── scripts/           # CLI entry points (init-db, ingest, export)
├── data/
│   ├── raw/           # Source files (gitignored)
│   └── processed/     # Cleaned outputs (gitignored)
├── tests/             # Test suite
└── docs/              # Documentation
```

## Quick start

```bash
# Set up environment
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Initialize the database
python scripts/init_db.py

# Ingest a long-format CSV (creates the schema if needed)
python scripts/ingest.py tests/fixtures/sample_incidents.csv --db fhab.db
```

The ingest is **idempotent** — re-running the same file updates existing rows
rather than duplicating them. Pass `--strict` to exit non-zero if any row fails
validation. See [`fhab.ingest`](src/fhab/ingest.py) for the expected CSV columns.

## Status

Early scaffold. The current relational core implements a Tier 3-shaped model; the
full tiered framework is not yet built.

- [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) — business requirements: the external
  three-tier ingestion model (IoW / CA State Water Boards Phase 1) **and** the internal
  CRM case-management lifecycle.
- [docs/DATA_MODEL_CA_FHAB.md](docs/DATA_MODEL_CA_FHAB.md) — the authoritative target:
  the published CA FHAB model (Report → Case → Response → Result + Advisory) and its
  four flat files on the California Open Data Portal.
- [docs/SCHEMA_PROPOSAL.md](docs/SCHEMA_PROPOSAL.md) — **proposed** PostgreSQL + PostGIS
  redesign around the CRM lifecycle (under review; not yet applied).
- [docs/GEOCONNEX.md](docs/GEOCONNEX.md) — persistent URL identifiers for HAB locations
  via Geoconnex, and HUC-12 watershed linkage.
- [docs/LEGACY_SCHEMA_REVIEW.md](docs/LEGACY_SCHEMA_REVIEW.md) — analysis of the existing
  (problematic) database that produces the open data: what it validates, what to adopt,
  and the anti-patterns to fix.
- [docs/GAP_ANALYSIS.md](docs/GAP_ANALYSIS.md) — how the current schema maps to those
  requirements and what's next.
- [docs/SCHEMA.md](docs/SCHEMA.md) — the current (scaffold) data model.

Pull the published CA FHAB reference data (flat files + data dictionary) into
`data/raw/` for development:

```bash
python scripts/fetch_reference_data.py
```

## License

MIT — see [LICENSE](LICENSE).
