# Schema Proposal — Modernized CA FHAB (PostgreSQL + PostGIS)

**Status: proposal for review. Not yet applied to `sql/schema.sql`.**

This proposes replacing the current SQLite scaffold
(`waterbody/site/sample/result/advisory`) with a normalized **PostgreSQL + PostGIS**
model built around the authoritative CA FHAB CRM lifecycle
([DATA_MODEL_CA_FHAB.md](DATA_MODEL_CA_FHAB.md)) and satisfying the requirements in
[REQUIREMENTS.md](REQUIREMENTS.md).

Target engine: **PostgreSQL 15+ with PostGIS** — required for HUC-12 point-in-polygon
derivation ([GEOCONNEX.md](GEOCONNEX.md) §4), spatial map serving, and cloud hosting
(`MGT-6`).

## Design principles

1. **Four CRM entities are the spine:** `report → case → response → result`, plus
   `advisory` (`CRM-1..5`). Their CA IDs (`Bloom_Report_ID`, `Case_ID`,
   `Response_Action_ID`, `Result_ID`, `Advisory_ID`) are preserved as the public keys.
2. **Store normalized; publish denormalized.** The four flat files and the geoconnex PID
   CSV are *generated views* (`DIS-2a`), not stored tables. Derived counts/flags
   (`Number_of_Blooms_Linked_to_Case`, `Lab_Data_Linked_to_Bloom`, …) are computed at
   export time.
3. **Geospatial-first.** Every observation/site carries a PostGIS point; watershed and
   geoconnex linkage hang off geometry.
4. **External ingestion feeds the lifecycle.** Tier 1–3 contributor data
   (`COL-T1..T3`) lands as `report`s / `observation`s; it does not get its own parallel
   universe.

## Entity overview

```
                 ┌───────────┐      ┌──────────┐
                 │ waterbody │◄─────│  huc12   │ (USGS WBD reference)
                 └─────┬─────┘      └──────────┘
                       │
        ┌──────────────┼───────────────┐
        ▼              ▼                ▼
   ┌─────────┐   ┌───────────┐    ┌──────────────┐
   │  case   │◄──│  report   │    │monitoring_site│ (Tier 3, geoconnex PID)
   └────┬────┘   └─────┬─────┘    └──────┬───────┘
        │              │                 │
        ▼              ▼                 ▼
   ┌─────────┐    ┌─────────┐       ┌─────────┐
   │ response│───►│advisory │       │ sample  │──► result ──► analyte (taxonomy)
   └─────────┘    └─────────┘       └─────────┘
        ▲                                ▲
        └───── hab_event (geoconnex PID) ┘   (confirmed bloom; ties case+location)
```

## Core tables (illustrative DDL)

```sql
CREATE EXTENSION IF NOT EXISTS postgis;

-- ---------- Reference / hydrography ----------

-- USGS Watershed Boundary Dataset HUC-12 polygons (loaded as reference data).
CREATE TABLE huc12 (
    huc12          char(12) PRIMARY KEY,
    name           text NOT NULL,
    geom           geometry(MultiPolygon, 4326) NOT NULL,
    geoconnex_uri  text GENERATED ALWAYS AS ('https://geoconnex.us/ref/hu12/' || huc12) STORED
);
CREATE INDEX huc12_geom_gix ON huc12 USING gist (geom);

CREATE TABLE waterbody (
    id                    bigserial PRIMARY KEY,
    name                  text NOT NULL,
    official_name         text,
    water_body_type       text,          -- lake, reservoir, wadeable stream, pond, …
    county                text,
    regional_water_board  text,
    water_body_manager    text,
    drinking_water_source text,          -- yes/no/unknown
    UNIQUE (name, county)
);

-- A point: either a fixed monitoring site OR an ad-hoc report/observation location.
CREATE TABLE location (
    id            bigserial PRIMARY KEY,
    waterbody_id  bigint REFERENCES waterbody(id),
    geom          geometry(Point, 4326),       -- lon/lat, WGS84
    datum         text,
    landmark      text,
    huc12         char(12) REFERENCES huc12(huc12)  -- derived by point-in-poly trigger
);
CREATE INDEX location_geom_gix ON location USING gist (geom);

-- ---------- CRM spine ----------

CREATE TYPE advisory_category AS ENUM ('none', 'caution', 'warning', 'danger');
CREATE TYPE case_status       AS ENUM ('open', 'closed');

-- CRM-2: staff organizational grouping of reports for a waterbody.
CREATE TABLE hab_case (
    id            bigint PRIMARY KEY,            -- = Case_ID
    waterbody_id  bigint REFERENCES waterbody(id),
    case_class    text,                          -- e.g. "Event Response"
    status        case_status NOT NULL DEFAULT 'open',
    case_lead     text,
    case_year     int,
    start_date    date,
    end_date      date,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- CRM-1: a suspected-HAB submission. No determination at submission.
CREATE TABLE report (
    id                       bigint PRIMARY KEY,   -- = Bloom_Report_ID
    case_id                  bigint REFERENCES hab_case(id),   -- null until triaged
    location_id              bigint REFERENCES location(id),
    report_type              text,                 -- "Public Reporting", partner, staff
    observation_date         date,
    reported_at              timestamptz,
    report_owner_first       text,
    report_owner_last        text,
    -- observed characteristics (reporter-supplied, uncleaned)
    bloom_type               text,
    bloom_size               text,
    bloom_location           text,
    bloom_texture            text,
    surface_water_condition  text,
    weather_condition        text,
    bloom_description        text,
    has_pictures             boolean DEFAULT false,
    created_at               timestamptz NOT NULL DEFAULT now()
);

-- CRM-3: a staff action on a report/case (investigation, advisory, etc.).
CREATE TABLE response (
    id                bigint PRIMARY KEY,          -- = Response_Action_ID
    case_id           bigint REFERENCES hab_case(id),
    report_id         bigint REFERENCES report(id),
    response_category text,                        -- e.g. "Advisory"
    response_type     text,
    performed_by      text,                        -- Response_Update_By
    performed_at      timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now()
);

-- CRM-5: advisory listing/delisting, issued via a response. History-preserving:
-- each create/update/end is a row; current state is the latest by recommended_at.
CREATE TABLE advisory (
    id               bigint PRIMARY KEY,           -- = Advisory_ID
    response_id      bigint NOT NULL REFERENCES response(id),
    waterbody_id     bigint REFERENCES waterbody(id),
    recommended      advisory_category NOT NULL,
    start_date       date,
    end_date         date,                         -- null = no confirmed senescence
    detail           text,
    spatial_extent   numeric,
    extent_unit      text,                         -- feet | other
    display_on_map   boolean NOT NULL DEFAULT false,  -- Display_Advisory_On_Map?
    recommended_at   timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now()
);

-- A confirmed/assessed bloom event — the thing geoconnex mints an event PID for and
-- that ties a case to a location over a date range. (The "event" in report/event/
-- response/case.)
CREATE TABLE hab_event (
    id             bigint PRIMARY KEY,
    case_id        bigint REFERENCES hab_case(id),
    location_id    bigint REFERENCES location(id),
    waterbody_id   bigint REFERENCES waterbody(id),
    first_observed date,
    last_observed  date,
    geoconnex_uri  text UNIQUE,   -- https://geoconnex.us/ca-fhab/events/{id}
    created_at     timestamptz NOT NULL DEFAULT now()
);

-- ---------- Analysis / results ----------

CREATE TYPE data_type AS ENUM (
    'Laboratory', 'Field Visual', 'Field Measurement',
    'Field Batch', 'Lab Batch', 'Veterinary'   -- Veterinary excluded from public export
);

-- Three-level analyte taxonomy (CRM-6): Analysis Type → Analyte Class → Analyte.
CREATE TABLE analyte (
    id            bigserial PRIMARY KEY,
    analysis_type text NOT NULL,   -- Cyanotoxin, Microscopy, Nutrient, Pigment, Genetic
    analyte_class text,            -- microcystin, taxa dominance, …
    analyte       text NOT NULL,   -- total microcystin, total nitrogen, mcyE, …
    default_unit  text,
    UNIQUE (analysis_type, analyte_class, analyte)
);

CREATE TABLE sample (
    id              bigserial PRIMARY KEY,
    report_id       bigint REFERENCES report(id),
    case_id         bigint REFERENCES hab_case(id),
    location_id     bigint REFERENCES location(id),
    sample_label    text,            -- Sample_ID (container tracking id)
    sample_type     text,            -- water, spatt, mat, …
    sample_date     date,
    collected_by    text
);

-- CRM-6: one analyte result per sample. Value may be numeric OR categorical.
CREATE TABLE result (
    id                bigint PRIMARY KEY,           -- = Result_ID
    sample_id         bigint NOT NULL REFERENCES sample(id),
    analyte_id        bigint NOT NULL REFERENCES analyte(id),
    data_type         data_type NOT NULL,
    method            text,
    measurement_value numeric,                      -- when quantitative
    measurement_text  text,                         -- when categorical (presence/absence)
    measurement_unit  text,                         -- e.g. ug/L, cells/mL
    taxa              text,
    results_date      date
);
```

## Derivations & triggers

- **`location.huc12`** populated on insert/update by a trigger doing
  `SELECT huc12 FROM huc12 WHERE ST_Contains(geom, NEW.geom)` (`COL-T1.4`).
- **`geoconnex_uri`** set when a site/event is created; immutable thereafter.
- **Denormalized export fields** (linked-data booleans, counts, repeated waterbody/case
  context) are produced by export queries, not stored.

## External ingestion (Tier 1–3) — how it attaches

- **Tier 1 Posts / Tier 2 Qualitative** land as `report` + `location` rows (flat,
  point-per-row), with custom Tier 2 parameters captured via an extensible
  `observation_parameter` table (a `parameter` registry + typed value), to be detailed
  alongside implementation.
- **Tier 3 Quantitative** lands as `monitoring_site` (fixed, geoconnex PID) + `sample` +
  `result`, reusing the analysis taxonomy above.
- A `review_status` enum + reviewer/comment on `report`/`observation` satisfies `MGT-1/2`
  (public records show review state).

These are sketched here and will be fleshed out in the implementation step so the
proposal stays focused on the CRM spine.

## Migration / build approach

1. Stand up Postgres + PostGIS (docker-compose for local dev).
2. Translate this DDL into `sql/schema.sql` (Postgres) with `sql/migrations/`.
3. Load USGS WBD HUC-12 for California into `huc12`.
4. Rewrite `fhab.db`/`fhab.ingest` for psycopg; add loaders that map the four published
   CSVs into the normalized model (round-trip: import → normalize → re-export, validated
   against the originals).
5. Add `fhab.export` producing the four flat files + the `ca-fhab` geoconnex CSV.
6. Stand up pygeoapi over the DB for OGC API – Features landing pages.

## Open questions for review

1. **HUC source** — USGS WBD HUC-12 unless you specify a CA-specific authority.
2. **ID strategy** — preserve CA integer IDs as PKs (shown here), or surrogate keys with
   CA IDs as unique business columns? Preserving them keeps exports trivially faithful.
3. **`hab_event` vs. case** — is "event" a distinct first-class entity (modeled here for
   geoconnex PIDs), or just a confirmed case? Confirm the intended `report/event/
   response/case` semantics.
4. **Advisory history** — model as append-only action rows (shown) vs. current-state +
   separate audit log.
5. **pygeoapi** for landing pages, or a custom JSON-LD endpoint?
