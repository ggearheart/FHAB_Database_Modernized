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

# Ingest a raw file
python scripts/ingest.py data/raw/incidents.csv
```

## Status

Early scaffold. See [docs/SCHEMA.md](docs/SCHEMA.md) for the proposed data model.

## License

MIT — see [LICENSE](LICENSE).
