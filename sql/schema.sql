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

DO $$ BEGIN
    CREATE TYPE advisory_category AS ENUM ('none', 'caution', 'warning', 'danger');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE case_status_enum AS ENUM ('open', 'closed');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE event_status_enum AS ENUM ('suspected', 'confirmed', 'not_a_hab');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE data_type_enum AS ENUM (
        'Laboratory', 'Field Visual', 'Field Measurement',
        'Field Batch', 'Lab Batch', 'Veterinary'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ---------- Reference / hydrography ----------

-- CA Water Boards "HUC Watersheds" feature service (HUC12 layer; USGS WBD republish).
CREATE TABLE IF NOT EXISTS huc12 (
    huc12          char(12) PRIMARY KEY,
    name           text NOT NULL,
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

-- A point: an ad-hoc report/observation location or a fixed monitoring site.
CREATE TABLE IF NOT EXISTS location (
    id            bigserial PRIMARY KEY,
    waterbody_id  bigint REFERENCES waterbody(id),
    geom          geometry(Point, 4326),
    bloom_datum   text,
    landmark      text,
    huc12         char(12) REFERENCES huc12(huc12)
);

-- ---------- Lifecycle spine ----------

-- CRM-2: staff organizational grouping of confirmed events for a waterbody.
CREATE TABLE IF NOT EXISTS hab_case (
    case_id            bigint PRIMARY KEY,
    waterbody_id       bigint REFERENCES waterbody(id),
    case_water_body_name text,
    case_class         text,
    case_status        case_status_enum,
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
    -- public-health (adopted from legacy review)
    illness_type             text,
    illness_description      text,
    geoconnex_uri            text UNIQUE,
    created_at               timestamptz NOT NULL DEFAULT now()
);
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
    advisory_recommended       advisory_category,
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

CREATE TABLE IF NOT EXISTS sample (
    id              bigserial PRIMARY KEY,
    bloom_report_id bigint REFERENCES event(bloom_report_id),
    case_id         bigint REFERENCES hab_case(case_id),
    location_id     bigint REFERENCES location(id),
    sample_id       text,        -- container/tracking label ("Sample_ID")
    sample_type     text,
    sample_location text,
    site            text,
    sample_date     date,
    coc_id          text         -- chain of custody (adopted from legacy review)
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
    results_date      date
);
