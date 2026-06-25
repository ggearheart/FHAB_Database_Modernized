# Gap Analysis — current scaffold vs. ingestion requirements

How today's schema and ETL ([SCHEMA.md](SCHEMA.md), [src/fhab/](../src/fhab))
measure against the [REQUIREMENTS.md](REQUIREMENTS.md) derived from the IoW Phase 1
framework. Status legend: ✅ met · 🟡 partial · ❌ missing.

## Summary

The current scaffold implements a **Tier 3-shaped relational core** (waterbody → site
→ sample → result) and a CSV ingest. It does **not** yet cover Tier 1/Tier 2 posts,
contributor-defined parameters, watershed derivation, review status, provenance/owner,
the API, or dissemination. The biggest structural decision ahead is how to host all
three tiers in one model.

| Area                          | Req                | Status | Notes |
|-------------------------------|--------------------|--------|-------|
| Tier 3 sites + time-series    | `COL-T3.1/.2`      | ✅ | `site` + `sample`/`result` give the one-to-many station→readings shape. |
| Tier 3 site metadata          | `COL-T3.1/.4`      | 🟡 | Have `site.name` + lat/long; missing `site_description`, QAPP/protocol/method metadata. |
| Tier 1 posts                  | `COL-T1`           | ❌ | No flat observation/post entity (comments, image, owner, point-per-row). |
| Tier 2 qualitative            | `COL-T2`           | ❌ | No contributor-defined custom parameters. |
| Extensible parameters         | `COL-T2.1/.2`      | ❌ | `result.analyte` is free text but there's no parameter registry, type system, or Document/Image types. |
| Watershed derivation          | `COL-T1.4`         | ❌ | No `watershed_huc`/`watershed_name`; no point-in-polygon (PostGIS) lookup. |
| Provenance / data owner       | `COL-T1`, `MGT-7`  | 🟡 | `sample.source` records a file name; no `report_owner`, contributing org, or ownership model. |
| QAQC review status            | `MGT-1/2/3`        | ❌ | No review/confirmation status field on observations. |
| Tier/type filter              | `MGT-4`            | ❌ | No tier dimension to filter on. |
| Validation                    | `MGT-1`            | 🟡 | `validate_sample` exists but is minimal (date/value/flag). |
| Cloud / Postgres              | `MGT-6`            | 🟡 | Schema is Postgres-portable by design but runs on local SQLite; HUC point-in-poly needs PostGIS. |
| API (GET, JSON, auth)         | `MGT-9..13`        | ❌ | No API layer at all. |
| Export (CEDEN/WQX/JSON-LD)    | `DIS-2/3`          | ❌ | No export command; advisory model is unrelated to these standards. |
| Visualizations / dashboards   | `DIS-1`            | ❌ | Out of scope for this repo so far. |
| Alerts                        | `DIS-4`            | ❌ | Not started. |
| Advisory model                | —                  | ➖ | `advisory` table exists in the scaffold but isn't called for by the framework; keep as State-side case-management context or drop. |

## Recommended next moves (proposed, not yet built)

1. **Introduce a tier dimension and a `post` (Tier 1) entity** so flat observations and
   relational readings coexist. Likely a shared `observation` supertype carrying
   geolocation, owner, watershed, image, and `review_status`, with Tier 3 readings
   linked to a `site`.
2. **Add an extensible parameter model** — a `parameter` registry (name, unit, data
   type ∈ {text, date, enumeration, integer, decimal, document, image}) plus a typed
   value table — to satisfy `COL-T2.1/.2` and `COL-T3.3`.
3. **Add provenance**: `organization` / `report_owner`, and a `review_status` enum with
   reviewer + comment for `MGT-2`.
4. **Watershed derivation** (`COL-T1.4`): on Postgres+PostGIS, derive HUC-12 by
   point-in-polygon; on SQLite, accept supplied values and defer the spatial join.
5. **Read-only JSON API** (`MGT-10..12`): `/sites/` and `/readings/` first, API-key auth.
6. **Exporters** (`DIS-2`): start with a generic machine-readable dump, then a
   WQX/CEDEN-aligned profile.

Items 1–3 are schema-level and should be settled before more ETL is built on the
current model.
