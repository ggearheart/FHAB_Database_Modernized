-- FHAB Database Modernized — core schema (PostgreSQL + PostGIS)
--
-- Normalized model for the CA FHAB CRM lifecycle. See docs/SCHEMA_PROPOSAL.md and
-- docs/DATA_MODEL_CA_FHAB.md. Column names mirror the published open-data fields
-- (lowercase snake_case); the exporter restores published header casing.
--
-- Lifecycle: report -> event -> case, with response relating to both events and cases.
-- Per program confirmation, the public Bloom_Report_ID equals the legacy BloomInfo_ID,
-- so report and event are 1:1 and share that integer id: the central `event` entity is
-- keyed by `bloom_report_id` and carries an `event_status` for its lifecycle stage.

CREATE EXTENSION IF NOT EXISTS postgis;

-- ---------- Enumerated types ----------

-- case_status and advisory_recommended are stored as text holding the verbatim published
-- controlled values, which are richer than a 4-state enum (per the case-management manual):
--   case_status: Open | Ongoing | Closed | Re-opened
--   advisory_recommended: None | Caution | Warning | Danger | Algal mat alert sign |
--     Algal mat general awareness sign | Visual observation | General awareness |
--     NA - refer to Report Details
-- Normalizing these into lookup tables is a planned refinement (docs/CASE_MANAGEMENT_RULES.md).

DO $$ BEGIN
    CREATE TYPE event_status_enum AS ENUM ('suspected', 'confirmed', 'not_a_hab');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE data_type_enum AS ENUM (
        'Laboratory', 'Field Visual', 'Field Measurement',
        'Field Batch', 'Lab Batch', 'Veterinary'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ---------- Report determination (outcome) lookup ----------
-- What a report turned out to be, set by staff after investigation. Staff-editable.
CREATE TABLE IF NOT EXISTS report_determination (
    code        text PRIMARY KEY,
    label       text NOT NULL,
    description text,
    sort_order  int NOT NULL DEFAULT 0
);

INSERT INTO report_determination (code, label, description, sort_order) VALUES
  ('under_investigation', 'Under investigation', 'Outcome not yet determined.', 1),
  ('confirmed_hab',       'Confirmed HAB (cyanobacteria)', 'Confirmed cyanobacterial harmful algal bloom.', 2),
  ('red_tide',            'Red tide (marine bloom)', 'Marine algal bloom / red tide.', 3),
  ('non_hab_algae',       'Non-HAB algae', 'Nuisance/non-toxic algae (e.g. azolla, green algae, other).', 4),
  ('spill',               'Spill / discharge', 'Potential spill or discharge to surface water.', 5),
  ('other_wq',            'Other water-quality issue', 'Other water-quality concern, not an algal bloom.', 6),
  ('no_bloom',            'No bloom / not an issue', 'No bloom present; not a water-quality concern.', 7)
ON CONFLICT (code) DO UPDATE SET label = EXCLUDED.label, description = EXCLUDED.description,
                                 sort_order = EXCLUDED.sort_order;

-- Recommended advisory vocabulary (drives the advisory dropdown and public-map symbology).
CREATE TABLE IF NOT EXISTS recommended_advisory (
    code       text PRIMARY KEY,
    label      text NOT NULL,
    sort_order int NOT NULL DEFAULT 0
);
INSERT INTO recommended_advisory (code, label, sort_order) VALUES
  ('None', 'None (no advisory)', 1),
  ('Caution', 'Caution', 2),
  ('Warning', 'Warning', 3),
  ('Danger', 'Danger', 4),
  ('Algal mat alert sign', 'Algal mat alert', 5),
  ('Algal mat general awareness sign', 'Algal mat general awareness', 6),
  ('Visual observation', 'Visual observation', 7),
  ('General awareness', 'General awareness', 8),
  ('NA - refer to Report Details', 'NA — refer to report details', 9)
ON CONFLICT (code) DO UPDATE SET label = EXCLUDED.label, sort_order = EXCLUDED.sort_order;

-- ---------- Reference / hydrography ----------

-- CA Water Boards "HUC Watersheds" feature service (HUC12 layer; USGS WBD republish).
CREATE TABLE IF NOT EXISTS huc12 (
    huc12          char(12) PRIMARY KEY,
    name           text,
    hutype         text,
    tohuc          char(12),
    areasqkm       double precision,
    geom           geometry(MultiPolygon, 4326),
    geoconnex_uri  text GENERATED ALWAYS AS ('https://geoconnex.us/ref/hu12/' || huc12) STORED
);

CREATE TABLE IF NOT EXISTS waterbody (
    id                       bigserial PRIMARY KEY,
    water_body_name          text NOT NULL,
    official_water_body_name text,
    water_body_type          text,
    county                   text,
    regional_water_board     text,
    water_body_manager       text,
    drinking_water_source    text,
    UNIQUE (water_body_name, county)
);

CREATE INDEX IF NOT EXISTS huc12_geom_gix ON huc12 USING gist (geom);

-- A point: an ad-hoc report/observation location or a fixed monitoring site.
CREATE TABLE IF NOT EXISTS location (
    id            bigserial PRIMARY KEY,
    waterbody_id  bigint REFERENCES waterbody(id),
    geom          geometry(Point, 4326),
    bloom_datum   text,
    landmark      text,
    huc12         char(12) REFERENCES huc12(huc12)
);
CREATE INDEX IF NOT EXISTS location_geom_gix ON location USING gist (geom);

-- ---------- Lifecycle spine ----------

-- CRM-2: staff organizational grouping of confirmed events for a waterbody.
CREATE TABLE IF NOT EXISTS hab_case (
    case_id            bigint PRIMARY KEY,
    waterbody_id       bigint REFERENCES waterbody(id),
    case_water_body_name text,
    case_class         text,
    case_status        text,         -- Open | Ongoing | Closed | Re-opened
    case_lead          text,
    case_year          int,
    case_start_date    date,
    case_end_date      date,
    case_datetimestamp timestamptz,
    created_at         timestamptz NOT NULL DEFAULT now()
);

-- CRM-1/1a: the central bloom record (= legacy tbl_BloomInfo). Keyed by the published
-- Bloom_Report_ID. report -> event are 1:1 here; event_status carries the lifecycle stage.
CREATE TABLE IF NOT EXISTS event (
    bloom_report_id          bigint PRIMARY KEY,
    case_id                  bigint REFERENCES hab_case(case_id),
    location_id              bigint REFERENCES location(id),
    event_status             event_status_enum NOT NULL DEFAULT 'suspected',
    determination_code       text REFERENCES report_determination(code),  -- outcome (NULL = not recorded)
    report_type              text,
    observation_date         date,
    bloom_date_created       timestamptz,
    -- observed characteristics (reporter-supplied)
    bloom_type               text,
    bloom_size               text,
    bloom_location           text,
    bloom_texture            text,
    surface_water_condition  text,
    weather_condition        text,
    bloom_description        text,
    reported_advisory_types  text,
    has_pictures             boolean,
    signs_posted             text,          -- advisory signage observed (Caution/Danger/Warning/...)
    bloom_textures           text[],        -- multi-select textures from the public form
    management_comments      text,          -- internal (agency) notes
    -- public-health (adopted from legacy review)
    illness_type             text,
    illness_description      text,
    no_illness_observed      boolean,
    -- reporter contact (PII — withheld from the public map/exports)
    reporter_name            text,
    reporter_email           text,
    reporter_phone           text,
    reporter_org             text,
    geoconnex_uri            text UNIQUE,
    owner_org                text,   -- contributor org that owns this row (NULL = State)
    created_at               timestamptz NOT NULL DEFAULT now()
);
-- Suspected illness/death matrix (subject x illness/death) from the public bloom-report form.
-- Sensitive: internal-read only (see access_control.sql).
CREATE TABLE IF NOT EXISTS report_illness (
    id              bigserial PRIMARY KEY,
    bloom_report_id bigint NOT NULL REFERENCES event(bloom_report_id) ON DELETE CASCADE,
    subject         text NOT NULL,   -- Human, Dog, Pet, Fish, Wildlife, Cattle, Goat, Horse, Sheep, Livestock
    illness         boolean NOT NULL DEFAULT false,
    death           boolean NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS report_illness_report_idx ON report_illness(bloom_report_id);

-- Photos attached to a report (stored in-DB for the demo; swap for object storage in prod).
CREATE TABLE IF NOT EXISTS report_photo (
    id              bigserial PRIMARY KEY,
    bloom_report_id bigint NOT NULL REFERENCES event(bloom_report_id) ON DELETE CASCADE,
    filename        text,
    content_type    text,
    data            bytea NOT NULL,
    uploaded_by     bigint,
    uploaded_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS report_photo_report_idx ON report_photo(bloom_report_id);
CREATE INDEX IF NOT EXISTS event_case_idx ON event(case_id);

-- CRM-3: a staff action; relates to both events and cases (either/both set).
CREATE TABLE IF NOT EXISTS response (
    response_action_id     bigint PRIMARY KEY,
    bloom_report_id        bigint REFERENCES event(bloom_report_id),
    case_id                bigint REFERENCES hab_case(case_id),
    response_category      text,
    response_type          text,
    response_update_by     text,
    response_datetimestamp timestamptz,
    created_at             timestamptz NOT NULL DEFAULT now(),
    CHECK (bloom_report_id IS NOT NULL OR case_id IS NOT NULL)
);

-- CRM-5: advisory listing/delisting, issued via a response. History-preserving.
CREATE TABLE IF NOT EXISTS advisory (
    advisory_id                bigint PRIMARY KEY,
    response_action_id         bigint REFERENCES response(response_action_id),
    advisory_recommended       text,  -- None | Caution | Warning | Danger | Algal mat … | etc.
    advisory_start_date        date,
    advisory_end_date          date,
    advisory_detail            text,
    spatial_extent_of_advisory numeric,
    extent_unit_of_measure     text,
    display_advisory_on_map    boolean,
    advisory_date_of_recommendation date,
    advisory_date              timestamptz,
    created_at                 timestamptz NOT NULL DEFAULT now()
);

-- ---------- Analysis / results ----------

-- Three-level analyte taxonomy (CRM-6): Analysis Type -> Analyte Class -> Analyte.
CREATE TABLE IF NOT EXISTS analyte (
    id            bigserial PRIMARY KEY,
    analysis_type text,
    analyte_class text,
    analyte       text,
    default_unit  text,
    UNIQUE (analysis_type, analyte_class, analyte)
);

-- Common HAB analytes seeded so the result-entry dropdown is populated on a fresh database
-- (the CEDEN / open-data loaders add more as they ingest).
INSERT INTO analyte (analysis_type, analyte_class, analyte, default_unit) VALUES
  ('Cyanotoxin', 'Microcystins', 'Microcystin', 'ug/L'),
  ('Cyanotoxin', 'Anatoxins', 'Anatoxin-a', 'ug/L'),
  ('Cyanotoxin', 'Cylindrospermopsin', 'Cylindrospermopsin', 'ug/L'),
  ('Cyanotoxin', 'Saxitoxin', 'Saxitoxin', 'ug/L'),
  ('Genetic', 'Toxin gene', 'mcyE gene', 'copies/mL'),
  ('Genetic', 'Cyanobacteria', 'Cyanobacteria 16S rRNA gene', 'copies/mL'),
  ('Microscopy', 'Taxa', 'Dominant taxon', NULL),
  ('Pigment', 'Chlorophyll', 'Chlorophyll a', 'ug/L'),
  ('Field Measurement', 'Physical', 'Water temperature', 'C'),
  ('Field Measurement', 'Physical', 'pH', 'pH'),
  ('Field Measurement', 'Physical', 'Dissolved oxygen', 'mg/L'),
  ('Field Measurement', 'Physical', 'Turbidity', 'NTU'),
  ('Field Measurement', 'Physical', 'Secchi depth', 'm')
ON CONFLICT (analysis_type, analyte_class, analyte) DO NOTHING;

CREATE TABLE IF NOT EXISTS sample (
    id              bigserial PRIMARY KEY,
    bloom_report_id bigint REFERENCES event(bloom_report_id),
    case_id         bigint REFERENCES hab_case(case_id),
    location_id     bigint REFERENCES location(id),
    station_id      bigint,      -- FK added after station table (see ALTER below)
    sample_id       text,        -- container/tracking label ("Sample_ID")
    sample_type     text,
    sample_location text,
    site            text,
    sample_date     date,
    sample_time     time,
    collected_by    text,        -- field crew / collector
    owner_org       text,        -- contributor org that owns this sample (NULL = State)
    coc_id          text,        -- chain of custody (adopted from legacy review)
    -- CEDEN / Bend sample identity (populated from the Bend->CEDEN workflow).
    bg_id           text,        -- Bend Genetics per-sample id (e.g. WB6630)
    lab_sample_id   text,
    lab_batch       text,
    project_code    text,
    lab_agency_code text
);

-- CRM-6: one analyte result per sample. Value may be numeric or categorical.
-- "RESULT ID UNIQUE" (e.g. F1) is the genuinely unique row key; the published integer
-- Result_ID repeats across rows of the same result, so it is a plain column.
CREATE TABLE IF NOT EXISTS result (
    result_id_unique  text PRIMARY KEY,
    result_id         bigint,
    sample_id         bigint REFERENCES sample(id),
    analyte_id        bigint REFERENCES analyte(id),
    data_type         data_type_enum,
    measurement_type  text,
    method            text,
    measurement_value numeric,    -- when quantitative
    measurement_text  text,       -- when categorical (presence/absence)
    measurement_unit  text,
    taxa              text,
    lab               text,       -- LabCode / lab identity (adopted from legacy review)
    results_date      date,
    -- CEDEN chemistry fields (populated when filled from the Bend->CEDEN workflow).
    res_qual_code     text,       -- '=', 'ND', '<', …
    fraction_name     text,       -- 'Total'
    mdl               numeric,    -- method detection limit
    rl                numeric,    -- reporting limit
    qa_code           text,
    compliance_code   text,
    owner_org         text        -- contributor org that owns this result (NULL = State)
);

-- ---------- Stations & CEDEN linkage (docs/BEND_CEDEN_WORKFLOW.md) ----------

-- CEDEN station lookup list (StationCode -> coordinates); a reference registry used to
-- enrich station.geom. Loaded from scripts/fetch_ceden_stations.py output.
CREATE TABLE IF NOT EXISTS station_registry (
    station_code text PRIMARY KEY,
    station_name text,
    latitude     double precision,
    longitude    double precision,
    datum        text,
    source       text
);

-- Canonical monitoring station — the shared spine across Bend, CEDEN, and FHAB.
CREATE TABLE IF NOT EXISTS station (
    id            bigserial PRIMARY KEY,
    station_code  text UNIQUE,                 -- CEDEN StationCode (= Bend CustomerSample)
    station_name  text,
    waterbody_id  bigint REFERENCES waterbody(id),
    geom          geometry(Point, 4326),       -- enriched from a CEDEN station registry
    huc12         char(12) REFERENCES huc12(huc12),
    geoconnex_uri text UNIQUE,
    owner_org     text   -- contributor org that owns this station (NULL = State)
);
CREATE INDEX IF NOT EXISTS station_geom_gix ON station USING gist (geom);

-- Link a CEDEN station/sample to an FHAB event/case, with how + how sure (never silent).
CREATE TABLE IF NOT EXISTS sample_link (
    id              bigserial PRIMARY KEY,
    sample_id       bigint REFERENCES sample(id),
    station_id      bigint REFERENCES station(id),
    bloom_report_id bigint REFERENCES event(bloom_report_id),
    case_id         bigint REFERENCES hab_case(case_id),
    match_method    text,        -- sampleid | station_date | spatial_temporal | name | manual
    confidence      numeric,     -- 0..1
    distance_m      numeric,
    reviewed_by     text,
    reviewed_at     timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- ---------- Lab-batch reconciliation (full CEDEN chemistry template) ----------
-- Staging area for an uploaded batch that links to events only by StationCode + date.
-- Nothing here touches live sample/result until a staffer (or an auto-match) confirms a link.
CREATE TABLE IF NOT EXISTS lab_batch (
    id             bigserial PRIMARY KEY,
    filename       text,
    uploaded_by    bigint,
    uploaded_at    timestamptz NOT NULL DEFAULT now(),
    match_radius_m integer NOT NULL DEFAULT 2000,   -- spatial window for candidate events
    match_days     integer NOT NULL DEFAULT 14,     -- temporal window (+/- days)
    n_groups       integer DEFAULT 0,
    n_results      integer DEFAULT 0,
    status         text NOT NULL DEFAULT 'open'      -- open | done
);
CREATE TABLE IF NOT EXISTS lab_stage_sample (
    id             bigserial PRIMARY KEY,
    batch_id       bigint NOT NULL REFERENCES lab_batch(id) ON DELETE CASCADE,
    station_code   text,
    location_code  text,
    replicate      text,
    sample_date    date,
    sample_time    time,
    sample_type    text,
    lab_sample_id  text,
    lab_batch_code text,
    project_code   text,
    agency_code    text,
    station_id     bigint REFERENCES station(id),   -- resolved from station_code
    status         text NOT NULL DEFAULT 'unmatched', -- unmatched | linked | skipped
    linked_event   bigint REFERENCES event(bloom_report_id),
    linked_case    bigint REFERENCES hab_case(case_id),
    linked_sample  bigint REFERENCES sample(id),     -- the materialized live sample
    decided_by     bigint,
    decided_at     timestamptz
);
CREATE TABLE IF NOT EXISTS lab_stage_result (
    id              bigserial PRIMARY KEY,
    stage_sample_id bigint NOT NULL REFERENCES lab_stage_sample(id) ON DELETE CASCADE,
    analyte_name    text,
    method_name     text,
    fraction_name   text,
    unit_name       text,
    result          text,
    res_qual_code   text,
    mdl             text,
    rl              text,
    qa_code         text,
    dilution_factor text,
    result_comments text
);
CREATE INDEX IF NOT EXISTS lab_stage_sample_batch_idx ON lab_stage_sample(batch_id);
CREATE INDEX IF NOT EXISTS lab_stage_result_sample_idx ON lab_stage_result(stage_sample_id);

-- The sample.station_id FK and the bg_id unique index live in migrations.sql, so they run
-- after those columns are guaranteed to exist (safe on databases predating those columns).
