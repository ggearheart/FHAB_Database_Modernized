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

1. **Backwards-compatible field names.** Column names mirror the published open-data
   report fields so prior work and downstream consumers keep working. Postgres folds
   identifiers to lowercase, so we use the lowercase snake_case form of each published
   header (`bloom_report_id`, `water_body_name`, `regional_water_board`, …); the
   exporter restores the exact published header casing (`Bloom_Report_ID`,
   `Water_Body_Name`, …) via a name map.
2. **Preserve the integer IDs.** The CA IDs are the primary keys, not surrogates:
   `bloom_report_id`, `case_id`, `response_action_id`, `result_id`, `advisory_id`
   (plus a new internal `event_id`). This keeps exports trivially faithful.
3. **Five lifecycle entities are the spine** (see below). Store normalized; **publish
   denormalized** — the four flat files and the geoconnex PID CSV are *generated views*
   (`DIS-2a`); derived counts/flags are computed at export time.
4. **Geospatial-first.** Every observation/site carries a PostGIS point; watershed and
   geoconnex linkage hang off geometry.
5. **External ingestion feeds the lifecycle.** Tier 1–3 contributor data (`COL-T1..T3`)
   lands as `report`s; it does not get its own parallel universe.

## The lifecycle

Per the program's intended semantics:

> **All pathways start with a `report`.** A report may be triaged into an `event`
> (a distinct, first-class bloom occurrence). *Some — but not all —* events are
> confirmed as **actual HAB events**. *Some* of those are organized into a `case`.
> A `response` relates to **both events and cases**. Advisories are issued via responses;
> results (field/lab analysis) are the supporting evidence.

```
  report ──triage──► event ──confirm──► (actual HAB event) ──organize──► case
 (entry point)      (1st-class,                                          (groups ≥1
   many reports      suspected→                                           events for a
   may share an      confirmed→                                           waterbody)
   event)            not_a_hab)
                        ▲                                                    ▲
                        └──────────────── response ──────────────────────────┘
                                   (relates to event AND/OR case;
                                    issues advisory; logs actions)
                                              │
                          report / event / case ──► sample ──► result ──► analyte
```

```
                 ┌───────────┐      ┌──────────────────────┐
                 │ waterbody │◄─────│ huc12  (CA WB HUC_    │
                 └─────┬─────┘      │  Watersheds / WBD)    │
                       │            └──────────────────────┘
        ┌──────────────┼───────────────┐
        ▼              ▼                ▼
   ┌────────┐    ┌──────────┐     ┌───────────────┐
   │ report │───►│  event   │◄────│monitoring_site│ (Tier 3 fixed; geoconnex PID)
   └────────┘    └────┬─────┘     └───────┬───────┘
                      │ (geoconnex PID)   │
                      ▼                   ▼
                 ┌────────┐          ┌─────────┐
                 │  case  │          │ sample  │──► result ──► analyte (taxonomy)
                 └────┬───┘          └─────────┘
                      ▼
                 ┌─────────┐    ┌──────────┐
                 │ response│───►│ advisory │
                 └─────────┘    └──────────┘
```

## Core tables (illustrative DDL)

> Field names follow principle 1 (lowercase snake_case of the published headers).

```sql
CREATE EXTENSION IF NOT EXISTS postgis;

-- ---------- Reference / hydrography ----------

-- CA Water Boards "HUC Watersheds" feature service (HUC12 layer), which republishes the
-- USGS Watershed Boundary Dataset. Fields mirror that layer (huc12, name, hutype, tohuc…).
-- Source item: gispublic.waterboards.ca.gov  id=b6c1bab9acc148e7ac726e33c43402ee
CREATE TABLE huc12 (
    huc12          char(12) PRIMARY KEY,
    name           text NOT NULL,
    hutype         text,
    tohuc          char(12),
    areasqkm       double precision,
    geom           geometry(MultiPolygon, 4326) NOT NULL,
    geoconnex_uri  text GENERATED ALWAYS AS ('https://geoconnex.us/ref/hu12/' || huc12) STORED
);
CREATE INDEX huc12_geom_gix ON huc12 USING gist (geom);

CREATE TABLE waterbody (
    id                     bigserial PRIMARY KEY,
    water_body_name        text NOT NULL,
    official_water_body_name text,
    water_body_type        text,          -- lake, reservoir, wadeable stream, pond, …
    county                 text,
    regional_water_board   text,
    water_body_manager     text,
    drinking_water_source  text,          -- yes/no/unknown
    UNIQUE (water_body_name, county)
);

-- A point: an ad-hoc report/observation location OR a fixed monitoring site.
CREATE TABLE location (
    id            bigserial PRIMARY KEY,
    waterbody_id  bigint REFERENCES waterbody(id),
    geom          geometry(Point, 4326),       -- lon/lat, WGS84
    bloom_datum   text,
    landmark      text,
    huc12         char(12) REFERENCES huc12(huc12)  -- derived by point-in-poly trigger
);
CREATE INDEX location_geom_gix ON location USING gist (geom);

-- ---------- Lifecycle spine ----------

CREATE TYPE advisory_category AS ENUM ('none', 'caution', 'warning', 'danger');
CREATE TYPE case_status_enum  AS ENUM ('open', 'closed');
CREATE TYPE event_status_enum AS ENUM ('suspected', 'confirmed', 'not_a_hab');

-- CRM-2: staff organizational grouping of one or more (confirmed) events for a waterbody.
CREATE TABLE hab_case (
    case_id          bigint PRIMARY KEY,
    waterbody_id     bigint REFERENCES waterbody(id),
    case_class       text,                       -- e.g. "Event Response"
    case_status      case_status_enum NOT NULL DEFAULT 'open',
    case_lead        text,
    case_year        int,
    case_start_date  date,
    case_end_date    date,
    case_datetimestamp timestamptz NOT NULL DEFAULT now(),
    created_at       timestamptz NOT NULL DEFAULT now()
);

-- "Event" (first-class, internal). A distinct bloom occurrence; report→event is triage.
-- Some events become actual HAB events (status='confirmed'); some of those join a case.
CREATE TABLE event (
    event_id       bigserial PRIMARY KEY,
    case_id        bigint REFERENCES hab_case(case_id),  -- null until organized into a case
    waterbody_id   bigint REFERENCES waterbody(id),
    location_id    bigint REFERENCES location(id),
    event_status   event_status_enum NOT NULL DEFAULT 'suspected',
    first_observed date,
    last_observed  date,
    geoconnex_uri  text UNIQUE,   -- https://geoconnex.us/ca-fhab/events/{event_id}
    created_at     timestamptz NOT NULL DEFAULT now()
);

-- CRM-1: the entry point. A suspected-HAB submission; no determination at submission.
-- Many reports may be triaged onto one event.
CREATE TABLE report (
    bloom_report_id          bigint PRIMARY KEY,
    event_id                 bigint REFERENCES event(event_id),  -- null until triaged
    location_id              bigint REFERENCES location(id),
    report_type              text,                 -- "Public Reporting", partner, staff
    observation_date         date,
    bloom_date_created        timestamptz,
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
    reported_advisory_types  text,
    has_pictures             boolean DEFAULT false,
    created_at               timestamptz NOT NULL DEFAULT now()
);

-- CRM-3: a staff action; relates to BOTH events and cases (either/both may be set).
CREATE TABLE response (
    response_action_id bigint PRIMARY KEY,
    event_id           bigint REFERENCES event(event_id),
    case_id            bigint REFERENCES hab_case(case_id),
    response_category  text,                        -- e.g. "Advisory"
    response_type      text,
    response_update_by text,                        -- staff attribution
    response_datetimestamp timestamptz,
    created_at         timestamptz NOT NULL DEFAULT now(),
    CHECK (event_id IS NOT NULL OR case_id IS NOT NULL)
);

-- CRM-5: advisory listing/delisting, issued via a response. History-preserving:
-- each create/update/end is a row; current state is the latest by advisory_date.
CREATE TABLE advisory (
    advisory_id              bigint PRIMARY KEY,
    response_action_id       bigint NOT NULL REFERENCES response(response_action_id),
    advisory_recommended     advisory_category NOT NULL,
    advisory_start_date      date,
    advisory_end_date        date,                 -- null = no confirmed senescence
    advisory_detail          text,
    spatial_extent_of_advisory numeric,
    extent_unit_of_measure   text,                 -- feet | other
    display_advisory_on_map  boolean NOT NULL DEFAULT false,
    advisory_date_of_recommendation date,
    advisory_date            timestamptz,
    created_at               timestamptz NOT NULL DEFAULT now()
);

-- ---------- Analysis / results ----------

CREATE TYPE data_type_enum AS ENUM (
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
    bloom_report_id bigint REFERENCES report(bloom_report_id),
    event_id        bigint REFERENCES event(event_id),
    case_id         bigint REFERENCES hab_case(case_id),
    location_id     bigint REFERENCES location(id),
    sample_id       text,            -- container/tracking label (published "Sample_ID")
    sample_type     text,            -- water, spatt, mat, …
    sample_date     date,
    collected_by    text
);

-- CRM-6: one analyte result per sample. Value may be numeric OR categorical.
CREATE TABLE result (
    result_id         bigint PRIMARY KEY,
    sample_id         bigint NOT NULL REFERENCES sample(id),
    analyte_id        bigint NOT NULL REFERENCES analyte(id),
    data_type         data_type_enum NOT NULL,
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
  `SELECT huc12 FROM huc12 WHERE ST_Contains(geom, NEW.geom)` (`COL-T1.4`, `GEO-4`).
- **`geoconnex_uri`** set when a site/event is created; immutable thereafter (`GEO-1/2`).
- **Published flat files** are rebuilt by export queries that re-join the normalized
  model and restore the exact published header names. On the `report` flat file,
  `Case_ID` is derived through `report.event → event.case_id`; the linked-data booleans
  and counts (`Lab_Data_Linked_to_Bloom`, `Number_of_Blooms_Linked_to_Case`, …) are
  computed, not stored.

## External ingestion (Tier 1–3) — how it attaches

- **Tier 1 Posts / Tier 2 Qualitative** land as `report` + `location` rows; Tier 2 custom
  parameters via an extensible `observation_parameter` table (a `parameter` registry +
  typed value), detailed at implementation time.
- **Tier 3 Quantitative** lands as `monitoring_site` (fixed, geoconnex PID) + `sample` +
  `result`, reusing the analyte taxonomy.
- A `review_status` enum + reviewer/comment on `report` satisfies `MGT-1/2`.

## Migration / build approach

1. Stand up Postgres + PostGIS (docker-compose for local dev).
2. Translate this DDL into `sql/schema.sql` (Postgres) with `sql/migrations/`.
3. Load the CA Water Boards **HUC_Watersheds / HUC12** layer into `huc12`.
4. Rewrite `fhab.db`/`fhab.ingest` for psycopg; add loaders mapping the four published
   CSVs into the normalized model (round-trip: import → normalize → re-export, validated
   byte-for-field against the originals).
5. Add `fhab.export` producing the four flat files + the `ca-fhab` geoconnex CSV.
6. Stand up pygeoapi over the DB for OGC API – Features landing pages.

## Resolved from prior review

- **HUC source:** CA Water Boards **HUC_Watersheds** feature service (HUC12), id
  `b6c1bab9acc148e7ac726e33c43402ee`. ✅
- **Field names:** mirror the open-data reports for backwards compatibility. ✅
- **Integer IDs:** preserved as primary keys. ✅
- **Event semantics:** `event` is first-class; `report → event → case`; `response`
  relates to both events and cases. ✅

## Remaining open questions

1. **Event ID exposure** — `event_id` is a new internal ID (not in today's flat files).
   Should events get their own published flat file / geoconnex collection, or stay
   internal and surface only via the existing report/case/response/result exports?
2. **Advisory history** — append-only action rows (shown) vs. current-state + audit log?
3. **`monitoring_site` vs `location`** — fold fixed Tier 3 sites into `location` with a
   flag, or keep a separate `monitoring_site` table (shown in diagram)?
4. **pygeoapi** for landing pages, or a custom JSON-LD endpoint?
