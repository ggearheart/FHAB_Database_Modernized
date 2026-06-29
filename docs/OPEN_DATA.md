# Open data: the published flat files + the provisional API

The app regenerates the files published on
[data.ca.gov](https://data.ca.gov/dataset/surface-water-freshwater-harmful-algal-blooms)
directly from the live database, and serves a provisional read-only API.

| Dataset | Slug | Source |
|---|---|---|
| FHAB Bloom Reports | `bloom-report` | `event` + `location` + `waterbody` |
| FHAB Cases | `hab-cases` | `hab_case` |
| FHAB Responses | `hab-responses` | `response` + `advisory` |
| FHAB Results | `hab-results` | `result` + `sample` + `analyte` (veterinary excluded) |
| CEDEN Chemistry Results | `chemistry-results` | analyte results in the [CEDEN Surface Water Chemistry](https://data.ca.gov/dataset/surface-water-chemistry-results) structure |
| FHAB ↔ CEDEN Crosswalk | `chemistry-crosswalk` | links each chemistry result to the geospatial backbone + FHAB report/case |

All outputs use the **published column names** only. Reporter contact, the suspected
illness/death matrix, and veterinary results are **never** included — the export selects an
explicit column allowlist (`fhab.export`), so new internal/PII fields can't leak.

## CEDEN chemistry + crosswalk

`chemistry-results` emits the analyte results in the statewide **CEDEN Surface Water Chemistry
Results** structure (StationCode, SampleDate, AnalyteName, MatrixName, MethodName, Result,
ResQualCode, MDL, RL, QACode, TargetLatitude/Longitude, …) — so HAB chemistry can sit alongside
the rest of CEDEN chemistry. Each row carries a **`ResultRowID`** (our unique result id).

`chemistry-crosswalk` is the join partner: one row per result, keyed by the same **`ResultRowID`**,
adding the **geospatial backbone** (`HUC12`, `Latitude`/`Longitude`, station & event `GeoConnex`)
and the **FHAB `Bloom_Report_ID` / `Case_ID`** *where they exist*. CEDEN chemistry users get the
clean station-based results; FHAB users join the crosswalk to recover report/case/watershed
context.

## Two delivery paths

**1. CSV download (manual migration to data.ca.gov)**
- Staff page: **Open data** in the nav (`/export`).
- Per-file: `GET /export/<slug>.csv` → `fhab_<slug>_<YYYY-MM-DD>.csv`.
- All four: `GET /export/all.zip`.

**2. Provisional JSON API (for apps like the CyanoSafe demo)**
- Index: `GET /api/open/index.json` — lists datasets + URLs.
- Per dataset: `GET /api/open/<slug>.json` →
  `{ provisional: true, generated_at, dataset, count, records: [...] }`.
- Read-only, **CORS-open** (`Access-Control-Allow-Origin: *`), cached 10 min.

> **Provisional vs published.** The official data.ca.gov files are a periodic, staff-reviewed
> release. This API reflects the **current** database — including recent reports not yet in an
> official release — so every record is flagged `provisional: true` and is subject to change as
> reports are verified. Same column schema as the published files.

## Scheduled generation (daily / weekly)

Generate the files on a schedule with the CLI:

```bash
python scripts/export_flatfiles.py /var/data/fhab_export          # plain names
python scripts/export_flatfiles.py /var/data/fhab_export --dated  # date-stamped subdir
```

It uses `DATABASE_URL`. Wire it to whatever scheduler you use:

- **Plain cron** (weekly, Mondays 06:00):
  ```cron
  0 6 * * 1  cd /path/to/FHAB_Database_Modernized && DATABASE_URL=... python scripts/export_flatfiles.py /var/data/fhab_export --dated
  ```
- **Render cron job** (add to `render.yaml`):
  ```yaml
  - type: cron
    name: fhab-export
    runtime: python
    schedule: "0 6 * * 1"      # weekly
    buildCommand: pip install -e ".[deploy]"
    startCommand: python scripts/export_flatfiles.py /var/data/fhab_export --dated
    envVars:
      - key: DATABASE_URL
        fromDatabase: { name: fhab-db, property: connectionString }
  ```

The JSON API needs no scheduling — it serves live data on request (with the 10-minute cache).
