# Data Model

The schema normalizes FHAB monitoring data into five core tables. The grain runs
from a physical place (waterbody → site) to an event (sample) to a measurement
(result), with advisories tracked separately against the waterbody.

```
waterbody ──< site ──< sample ──< result
     └──< advisory
```

| Table       | Grain                              | Notes                                          |
|-------------|------------------------------------|------------------------------------------------|
| `waterbody` | One row per named waterbody        | Unique on (name, county, state)                |
| `site`      | One row per monitoring location    | Holds lat/long; belongs to a waterbody         |
| `sample`    | One sampling event at a site/date  | Unique on (site_id, sample_date)               |
| `result`    | One analyte measurement per sample | e.g. microcystin µg/L; carries a detect flag   |
| `advisory`  | One posted advisory                | Tier: caution / warning / danger               |

## Design notes

- **SQLite first.** Zero-setup local development; the schema avoids vendor-specific
  types so it ports cleanly to PostgreSQL.
- **Dates as ISO-8601 text.** `YYYY-MM-DD` strings sort and compare correctly in
  SQLite and are unambiguous across tools.
- **Idempotent DDL.** `CREATE TABLE IF NOT EXISTS` means `init_db` is safe to re-run.
- **Cascading deletes.** Removing a waterbody removes its sites, samples, and results.

## Open questions

- Should `result` support multiple methods per analyte (e.g. ELISA vs LC-MS/MS)?
- Do we need a separate `lab` / `method` dimension table?
- How are historical advisories reconciled with sample data for the same event?
