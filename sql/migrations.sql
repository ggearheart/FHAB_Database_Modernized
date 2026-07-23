-- Idempotent forward migrations.
--
-- schema.sql uses CREATE TABLE IF NOT EXISTS, which does NOT add columns to tables that
-- already exist. So every column added to an existing table after its first deployment is
-- (re)asserted here with ADD COLUMN IF NOT EXISTS (a no-op when the column is already present).
-- apply_schema() runs this after schema.sql, so deploying brings an older database current
-- without a destructive reset. Columns are added without FK/constraints to keep this safe on
-- databases that already hold data; fresh databases get the full definitions from schema.sql.

-- event
ALTER TABLE event ADD COLUMN IF NOT EXISTS determination_code text;
ALTER TABLE event ADD COLUMN IF NOT EXISTS owner_org text;
ALTER TABLE event ADD COLUMN IF NOT EXISTS reported_advisory_types text;
ALTER TABLE event ADD COLUMN IF NOT EXISTS illness_type text;
ALTER TABLE event ADD COLUMN IF NOT EXISTS illness_description text;
ALTER TABLE event ADD COLUMN IF NOT EXISTS geoconnex_uri text;
ALTER TABLE event ADD COLUMN IF NOT EXISTS bloom_date_created timestamptz;
-- Fields adopted from the official MyWaterQuality bloom-report form.
ALTER TABLE event ADD COLUMN IF NOT EXISTS signs_posted text;          -- Caution/Danger/Warning/...
ALTER TABLE event ADD COLUMN IF NOT EXISTS bloom_textures text[];      -- multi-select textures
ALTER TABLE event ADD COLUMN IF NOT EXISTS no_illness_observed boolean;
ALTER TABLE event ADD COLUMN IF NOT EXISTS management_comments text;
-- Reporter contact (PII — withheld from the public map/exports).
ALTER TABLE event ADD COLUMN IF NOT EXISTS reporter_name text;
ALTER TABLE event ADD COLUMN IF NOT EXISTS reporter_email text;
ALTER TABLE event ADD COLUMN IF NOT EXISTS reporter_phone text;
ALTER TABLE event ADD COLUMN IF NOT EXISTS reporter_org text;

-- sample
ALTER TABLE sample ADD COLUMN IF NOT EXISTS station_id bigint;
ALTER TABLE sample ADD COLUMN IF NOT EXISTS sample_time time;
ALTER TABLE sample ADD COLUMN IF NOT EXISTS collected_by text;
ALTER TABLE sample ADD COLUMN IF NOT EXISTS owner_org text;
ALTER TABLE sample ADD COLUMN IF NOT EXISTS coc_id text;
ALTER TABLE sample ADD COLUMN IF NOT EXISTS bg_id text;
ALTER TABLE sample ADD COLUMN IF NOT EXISTS lab_sample_id text;
ALTER TABLE sample ADD COLUMN IF NOT EXISTS lab_batch text;
ALTER TABLE sample ADD COLUMN IF NOT EXISTS project_code text;
ALTER TABLE sample ADD COLUMN IF NOT EXISTS lab_agency_code text;
ALTER TABLE sample ADD COLUMN IF NOT EXISTS sample_location text;
ALTER TABLE sample ADD COLUMN IF NOT EXISTS site text;
-- Lab-data workboard (assignment + QA of the event/report/case link).
ALTER TABLE sample ADD COLUMN IF NOT EXISTS assigned_to bigint;
ALTER TABLE sample ADD COLUMN IF NOT EXISTS qa_status text;
ALTER TABLE sample ADD COLUMN IF NOT EXISTS qa_by bigint;
ALTER TABLE sample ADD COLUMN IF NOT EXISTS qa_at timestamptz;
ALTER TABLE sample ADD COLUMN IF NOT EXISTS qa_note text;
CREATE INDEX IF NOT EXISTS sample_assigned_idx ON sample(assigned_to);

-- result
ALTER TABLE result ADD COLUMN IF NOT EXISTS res_qual_code text;
ALTER TABLE result ADD COLUMN IF NOT EXISTS fraction_name text;
ALTER TABLE result ADD COLUMN IF NOT EXISTS mdl numeric;
ALTER TABLE result ADD COLUMN IF NOT EXISTS rl numeric;
ALTER TABLE result ADD COLUMN IF NOT EXISTS qa_code text;
ALTER TABLE result ADD COLUMN IF NOT EXISTS compliance_code text;
ALTER TABLE result ADD COLUMN IF NOT EXISTS owner_org text;
ALTER TABLE result ADD COLUMN IF NOT EXISTS lab text;

-- station
ALTER TABLE station ADD COLUMN IF NOT EXISTS owner_org text;
ALTER TABLE station ADD COLUMN IF NOT EXISTS geoconnex_uri text;
ALTER TABLE station ADD COLUMN IF NOT EXISTS datum text;          -- coordinate datum (NAD83/WGS84)

-- CEDEN MatrixName captured on ingest (result + lab-batch staging predate these columns).
ALTER TABLE result ADD COLUMN IF NOT EXISTS matrix_name text;
ALTER TABLE lab_stage_result ADD COLUMN IF NOT EXISTS matrix_name text;

-- advisory
ALTER TABLE advisory ADD COLUMN IF NOT EXISTS advisory_detail text;

-- (app_user.password_hash is migrated in access_control.sql, where app_user is defined.)

-- Constraints/indexes that depend on the columns ensured above (so they are safe on a
-- database that predated those columns).
DO $$ BEGIN
    ALTER TABLE sample ADD CONSTRAINT sample_station_fk
        FOREIGN KEY (station_id) REFERENCES station(id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE UNIQUE INDEX IF NOT EXISTS sample_bg_id_uq ON sample (bg_id) WHERE bg_id IS NOT NULL;

-- Speeds up the map's per-event advisory lookup and the report detail joins.
CREATE INDEX IF NOT EXISTS response_event_idx ON response (bloom_report_id);
CREATE INDEX IF NOT EXISTS sample_event_idx ON sample (bloom_report_id);
CREATE INDEX IF NOT EXISTS advisory_response_idx ON advisory (response_action_id);
CREATE INDEX IF NOT EXISTS location_geom_gix ON location USING gist (geom);

-- Trigram index for fuzzy waterbody-name matching (type-ahead + near-duplicate guard).
CREATE INDEX IF NOT EXISTS waterbody_name_trgm ON waterbody USING gin (water_body_name gin_trgm_ops);

-- Folder ingest (email attachments from Bend / partner labs): batch provenance + source files.
ALTER TABLE lab_batch ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'staged';
ALTER TABLE lab_batch ADD COLUMN IF NOT EXISTS source text;
ALTER TABLE lab_batch ADD COLUMN IF NOT EXISTS region text;
ALTER TABLE lab_batch ADD COLUMN IF NOT EXISTS n_samples integer DEFAULT 0;
ALTER TABLE lab_batch ADD COLUMN IF NOT EXISTS n_geocoded integer DEFAULT 0;
ALTER TABLE sample ADD COLUMN IF NOT EXISTS lab_batch_id bigint;
ALTER TABLE sample ADD COLUMN IF NOT EXISTS sampling_type text;    -- NULL | 'routine'
ALTER TABLE sample ADD COLUMN IF NOT EXISTS routine_subtype text;
DO $$ BEGIN
    ALTER TABLE sample ADD CONSTRAINT sample_lab_batch_fk
        FOREIGN KEY (lab_batch_id) REFERENCES lab_batch(id) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
CREATE INDEX IF NOT EXISTS sample_lab_batch_idx ON sample (lab_batch_id);
CREATE TABLE IF NOT EXISTS lab_batch_file (
    id           bigserial PRIMARY KEY,
    batch_id     bigint NOT NULL REFERENCES lab_batch(id) ON DELETE CASCADE,
    category     text,
    filename     text NOT NULL,
    content_type text,
    byte_size    integer,
    data         bytea NOT NULL,
    uploaded_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS lab_batch_file_batch_idx ON lab_batch_file(batch_id);
CREATE TABLE IF NOT EXISTS sample_station_link (
    id           bigserial PRIMARY KEY,
    sample_id    bigint NOT NULL REFERENCES sample(id) ON DELETE CASCADE,
    station_code text NOT NULL,
    station_name text,
    linked_by    bigint,
    linked_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (sample_id, station_code)
);
CREATE INDEX IF NOT EXISTS sample_station_link_sample_idx ON sample_station_link(sample_id);
CREATE TABLE IF NOT EXISTS app_setting (
    key        text PRIMARY KEY,
    value      text,
    updated_by bigint,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Public submission: full-form illness + community/partner attribution (table predates these).
ALTER TABLE public_report_submission ADD COLUMN IF NOT EXISTS no_illness_observed boolean;
ALTER TABLE public_report_submission ADD COLUMN IF NOT EXISTS illness_description text;
ALTER TABLE public_report_submission ADD COLUMN IF NOT EXISTS illness jsonb;
ALTER TABLE public_report_submission ADD COLUMN IF NOT EXISTS group_id bigint;
ALTER TABLE public_report_submission ADD COLUMN IF NOT EXISTS report_type text;
ALTER TABLE public_report_submission ADD COLUMN IF NOT EXISTS trusted boolean NOT NULL DEFAULT false;

-- Governance #2: locally-authored records get their externally-visible ids from a reserved high
-- range (>= 1e9), so app-created ids can NEVER collide with the smaller published/legacy ids that
-- imports (open-data loaders, data.ca.gov refresh) insert explicitly. This replaces the racy
-- `max(id)+1` allocation and removes the risk of the refresh conflating a local report with a
-- different published report that happened to share a low id. See docs/GOVERNANCE_REVIEW.md.
DO $$ BEGIN CREATE SEQUENCE app_event_id_seq    AS bigint START 1000000000 MINVALUE 1000000000; EXCEPTION WHEN duplicate_table THEN NULL; END $$;
DO $$ BEGIN CREATE SEQUENCE app_case_id_seq     AS bigint START 1000000000 MINVALUE 1000000000; EXCEPTION WHEN duplicate_table THEN NULL; END $$;
DO $$ BEGIN CREATE SEQUENCE app_response_id_seq AS bigint START 1000000000 MINVALUE 1000000000; EXCEPTION WHEN duplicate_table THEN NULL; END $$;
DO $$ BEGIN CREATE SEQUENCE app_advisory_id_seq AS bigint START 1000000000 MINVALUE 1000000000; EXCEPTION WHEN duplicate_table THEN NULL; END $$;
ALTER TABLE event    ALTER COLUMN bloom_report_id    SET DEFAULT nextval('app_event_id_seq');
ALTER TABLE hab_case ALTER COLUMN case_id            SET DEFAULT nextval('app_case_id_seq');
ALTER TABLE response ALTER COLUMN response_action_id SET DEFAULT nextval('app_response_id_seq');
ALTER TABLE advisory ALTER COLUMN advisory_id        SET DEFAULT nextval('app_advisory_id_seq');
-- Catch each sequence up to any existing app-range rows (idempotent; a no-op on a fresh database).
DO $$ DECLARE m bigint; BEGIN
  m := (SELECT max(bloom_report_id)    FROM event    WHERE bloom_report_id    >= 1000000000); IF m IS NOT NULL THEN PERFORM setval('app_event_id_seq', m);    END IF;
  m := (SELECT max(case_id)            FROM hab_case WHERE case_id            >= 1000000000); IF m IS NOT NULL THEN PERFORM setval('app_case_id_seq', m);     END IF;
  m := (SELECT max(response_action_id) FROM response WHERE response_action_id >= 1000000000); IF m IS NOT NULL THEN PERFORM setval('app_response_id_seq', m); END IF;
  m := (SELECT max(advisory_id)        FROM advisory WHERE advisory_id        >= 1000000000); IF m IS NOT NULL THEN PERFORM setval('app_advisory_id_seq', m); END IF;
END $$;

-- Derived authoritative geo attributes on station (point-in-polygon; see fhab.geo).
ALTER TABLE station ADD COLUMN IF NOT EXISTS county               text;
ALTER TABLE station ADD COLUMN IF NOT EXISTS regional_water_board text;

-- Governance #3: source-of-truth provenance. A staff correction to a published record must not
-- be silently reverted by the next data.ca.gov refresh. locally_edited is flipped true by a
-- human edit (see audit.sql flag_local_edit); the refresh skips rows where it is set.
ALTER TABLE event    ADD COLUMN IF NOT EXISTS locally_edited boolean NOT NULL DEFAULT false;
ALTER TABLE event    ADD COLUMN IF NOT EXISTS last_synced_at timestamptz;
ALTER TABLE event    ADD COLUMN IF NOT EXISTS source text;
ALTER TABLE hab_case ADD COLUMN IF NOT EXISTS locally_edited boolean NOT NULL DEFAULT false;
ALTER TABLE hab_case ADD COLUMN IF NOT EXISTS last_synced_at timestamptz;
ALTER TABLE hab_case ADD COLUMN IF NOT EXISTS source text;
ALTER TABLE response ADD COLUMN IF NOT EXISTS locally_edited boolean NOT NULL DEFAULT false;
ALTER TABLE response ADD COLUMN IF NOT EXISTS last_synced_at timestamptz;
ALTER TABLE response ADD COLUMN IF NOT EXISTS source text;
ALTER TABLE advisory ADD COLUMN IF NOT EXISTS locally_edited boolean NOT NULL DEFAULT false;
ALTER TABLE advisory ADD COLUMN IF NOT EXISTS last_synced_at timestamptz;
ALTER TABLE advisory ADD COLUMN IF NOT EXISTS source text;
