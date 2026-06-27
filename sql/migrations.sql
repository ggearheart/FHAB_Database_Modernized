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
