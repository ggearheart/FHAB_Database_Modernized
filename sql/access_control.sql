-- FHAB access control: users, roles, scoped role assignments, and Row-Level Security.
-- See docs/USER_ROLES.md. Applied after schema.sql.
--
-- Model: the application connects as the non-owning role `fhab_app` (read-only here) and
-- sets the current user with  SELECT set_config('fhab.user_id', <id>, false).  RLS policies
-- then filter rows by that user's roles + scopes. The DB owner (used by loaders/admin)
-- bypasses RLS, so back-end data loading is unaffected.

-- ---------- Application role ----------

DO $$ BEGIN
    CREATE ROLE fhab_app NOLOGIN;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ---------- Tables ----------

CREATE TABLE IF NOT EXISTS app_user (
    id             bigserial PRIMARY KEY,
    personnel_code text,
    email          text UNIQUE NOT NULL,
    full_name      text,
    is_active      boolean NOT NULL DEFAULT true,
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS role (
    code        text PRIMARY KEY,
    name        text NOT NULL,
    category    text NOT NULL,            -- internal_staff | contributor | manager | public
    description text
);

CREATE TABLE IF NOT EXISTS user_role (
    user_id            bigint NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    role_code          text   NOT NULL REFERENCES role(code),
    scope_region       text,
    scope_ddw_district text,
    scope_org          text,
    scope_waterbody_id bigint REFERENCES waterbody(id),
    granted_by         bigint REFERENCES app_user(id),
    granted_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, role_code, scope_region, scope_ddw_district, scope_org, scope_waterbody_id)
);
CREATE INDEX IF NOT EXISTS user_role_user_idx ON user_role(user_id);

-- ---------- Role catalog (seed) ----------

INSERT INTO role (code, name, category, description) VALUES
  ('program_admin',     'Program Administrator (OIMA)',          'internal_staff', 'Statewide program owner.'),
  ('wb_staff',          'Water Board Staff (Regional)',          'internal_staff', 'Core staff; region-scoped.'),
  ('illness_workgroup', 'Illness Workgroup Staff',               'internal_staff', 'Interagency HAB-related Illness Workgroup.'),
  ('viewer',            'Data Viewer (internal)',                'internal_staff', 'Read-only internal access.'),
  ('field_staff',       'Field Staff',                           'internal_staff', 'Optional: field data entry.'),
  ('lab_analyst',       'Lab Analyst',                           'internal_staff', 'Optional: lab data entry.'),
  ('ddw_staff',         'DDW Staff',                             'internal_staff', 'Optional: Division of Drinking Water.'),
  ('tribal_admin',      'Tribal Government Admin',               'contributor',    'Manages a Tribal monitoring program.'),
  ('comm_sci_manager',  'Community Science Program Manager',     'contributor',    'Manages a volunteer program.'),
  ('comm_sci_volunteer','Community Science Volunteer',           'contributor',    'Submits Tier 1-2 observations.'),
  ('partner_agency',    'Partner Agency',                        'contributor',    'Shares monitoring data (e.g. DWR).'),
  ('water_body_manager','Water Body Manager',                    'manager',        'Manages a waterbody; waterbody-scoped.'),
  ('decision_maker',    'Decision-Maker / Public Health Official','public',        'Elevated read + alerts.'),
  ('public',            'Public / Consumer',                     'public',         'Reads published advisories only.')
ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name, category = EXCLUDED.category;

-- ---------- Identity & scope functions ----------

CREATE OR REPLACE FUNCTION fhab_user_id() RETURNS bigint
  LANGUAGE sql STABLE AS $$ SELECT nullif(current_setting('fhab.user_id', true), '')::bigint $$;

CREATE OR REPLACE FUNCTION fhab_is_admin() RETURNS boolean
  LANGUAGE sql STABLE SECURITY DEFINER AS $$
    SELECT EXISTS (SELECT 1 FROM user_role WHERE user_id = fhab_user_id() AND role_code = 'program_admin') $$;

CREATE OR REPLACE FUNCTION fhab_is_internal() RETURNS boolean
  LANGUAGE sql STABLE SECURITY DEFINER AS $$
    SELECT EXISTS (SELECT 1 FROM user_role ur JOIN role r ON r.code = ur.role_code
                   WHERE ur.user_id = fhab_user_id() AND r.category = 'internal_staff') $$;

CREATE OR REPLACE FUNCTION fhab_unscoped_internal() RETURNS boolean
  LANGUAGE sql STABLE SECURITY DEFINER AS $$
    SELECT EXISTS (SELECT 1 FROM user_role ur JOIN role r ON r.code = ur.role_code
                   WHERE ur.user_id = fhab_user_id() AND r.category = 'internal_staff'
                     AND ur.scope_region IS NULL) $$;

CREATE OR REPLACE FUNCTION fhab_regions() RETURNS text[]
  LANGUAGE sql STABLE SECURITY DEFINER AS $$
    SELECT coalesce(array_agg(DISTINCT scope_region), '{}')
    FROM user_role WHERE user_id = fhab_user_id() AND scope_region IS NOT NULL $$;

CREATE OR REPLACE FUNCTION fhab_region_ok(region text) RETURNS boolean
  LANGUAGE sql STABLE AS $$
    SELECT fhab_unscoped_internal() OR (region IS NOT NULL AND region = ANY (fhab_regions())) $$;

CREATE OR REPLACE FUNCTION fhab_waterbody_ids() RETURNS bigint[]
  LANGUAGE sql STABLE SECURITY DEFINER AS $$
    SELECT coalesce(array_agg(scope_waterbody_id), '{}')
    FROM user_role WHERE user_id = fhab_user_id()
      AND role_code = 'water_body_manager' AND scope_waterbody_id IS NOT NULL $$;

-- Write helpers: staff who may edit (internal except read-only 'viewer'), and the
-- contributor organizations the user owns data for.
CREATE OR REPLACE FUNCTION fhab_is_staff_writer() RETURNS boolean
  LANGUAGE sql STABLE SECURITY DEFINER AS $$
    SELECT EXISTS (SELECT 1 FROM user_role ur JOIN role r ON r.code = ur.role_code
                   WHERE ur.user_id = fhab_user_id()
                     AND r.category = 'internal_staff' AND ur.role_code <> 'viewer') $$;

CREATE OR REPLACE FUNCTION fhab_user_orgs() RETURNS text[]
  LANGUAGE sql STABLE SECURITY DEFINER AS $$
    SELECT coalesce(array_agg(DISTINCT ur.scope_org), '{}')
    FROM user_role ur JOIN role r ON r.code = ur.role_code
    WHERE ur.user_id = fhab_user_id() AND r.category = 'contributor' AND ur.scope_org IS NOT NULL $$;

CREATE OR REPLACE FUNCTION fhab_location_region(locid bigint) RETURNS text
  LANGUAGE sql STABLE SECURITY DEFINER AS $$
    SELECT w.regional_water_board FROM location l JOIN waterbody w ON w.id = l.waterbody_id
    WHERE l.id = locid $$;

CREATE OR REPLACE FUNCTION fhab_waterbody_region(wbid bigint) RETURNS text
  LANGUAGE sql STABLE SECURITY DEFINER AS $$
    SELECT regional_water_board FROM waterbody WHERE id = wbid $$;

-- Cross-table helpers (SECURITY DEFINER = bypass RLS, no policy recursion).
CREATE OR REPLACE FUNCTION fhab_event_region(brid bigint) RETURNS text
  LANGUAGE sql STABLE SECURITY DEFINER AS $$
    SELECT w.regional_water_board FROM event e JOIN location l ON l.id = e.location_id
    JOIN waterbody w ON w.id = l.waterbody_id WHERE e.bloom_report_id = brid $$;

CREATE OR REPLACE FUNCTION fhab_event_waterbody(brid bigint) RETURNS bigint
  LANGUAGE sql STABLE SECURITY DEFINER AS $$
    SELECT l.waterbody_id FROM event e JOIN location l ON l.id = e.location_id
    WHERE e.bloom_report_id = brid $$;

CREATE OR REPLACE FUNCTION fhab_event_has_public_advisory(brid bigint) RETURNS boolean
  LANGUAGE sql STABLE SECURITY DEFINER AS $$
    SELECT EXISTS (SELECT 1 FROM response r JOIN advisory a ON a.response_action_id = r.response_action_id
                   WHERE r.bloom_report_id = brid AND a.display_advisory_on_map) $$;

CREATE OR REPLACE FUNCTION fhab_waterbody_has_public_advisory(wbid bigint) RETURNS boolean
  LANGUAGE sql STABLE SECURITY DEFINER AS $$
    SELECT EXISTS (SELECT 1 FROM event e JOIN location l ON l.id = e.location_id
                   JOIN response r ON r.bloom_report_id = e.bloom_report_id
                   JOIN advisory a ON a.response_action_id = r.response_action_id
                   WHERE l.waterbody_id = wbid AND a.display_advisory_on_map) $$;

CREATE OR REPLACE FUNCTION fhab_advisory_waterbody(aid bigint) RETURNS bigint
  LANGUAGE sql STABLE SECURITY DEFINER AS $$
    SELECT l.waterbody_id FROM advisory a JOIN response r ON r.response_action_id = a.response_action_id
    JOIN event e ON e.bloom_report_id = r.bloom_report_id JOIN location l ON l.id = e.location_id
    WHERE a.advisory_id = aid LIMIT 1 $$;

-- ---------- Row-Level Security policies ----------

-- waterbody, event, advisory: internal (region-scoped) + water body manager + public (published).
-- Internal staff are scoped to their region (admins see all). Non-internal users
-- (managers, public) get their waterbody and/or published rows. RLS defines the maximum
-- visible set; the app may apply tighter work-queue filters on top.
ALTER TABLE waterbody ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS waterbody_read ON waterbody;
CREATE POLICY waterbody_read ON waterbody FOR SELECT USING (
    fhab_is_admin()
    OR (fhab_is_internal() AND fhab_region_ok(regional_water_board))
    OR (NOT fhab_is_internal() AND (
        id = ANY (fhab_waterbody_ids()) OR fhab_waterbody_has_public_advisory(id)))
);

ALTER TABLE event ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS event_read ON event;
CREATE POLICY event_read ON event FOR SELECT USING (
    fhab_is_admin()
    OR (fhab_is_internal() AND fhab_region_ok(fhab_event_region(bloom_report_id)))
    OR (owner_org = ANY (fhab_user_orgs()))   -- contributor sees its own
    OR (NOT fhab_is_internal() AND (
        fhab_event_waterbody(bloom_report_id) = ANY (fhab_waterbody_ids())
        OR fhab_event_has_public_advisory(bloom_report_id)))
);

ALTER TABLE advisory ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS advisory_read ON advisory;
CREATE POLICY advisory_read ON advisory FOR SELECT USING (
    fhab_is_admin() OR fhab_is_internal()
    OR (NOT fhab_is_internal() AND (
        display_advisory_on_map
        OR fhab_advisory_waterbody(advisory_id) = ANY (fhab_waterbody_ids())))
);

-- hab_case: internal + manager (their waterbody).
ALTER TABLE hab_case ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS hab_case_read ON hab_case;
CREATE POLICY hab_case_read ON hab_case FOR SELECT USING (
    fhab_is_admin() OR fhab_is_internal() OR (waterbody_id = ANY (fhab_waterbody_ids()))
);

-- Contributor-owned tables: internal staff + the owning contributor org can read.
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['result','sample','station'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS %I_read ON %I', t, t);
        EXECUTE format(
            'CREATE POLICY %I_read ON %I FOR SELECT USING (
                 fhab_is_admin() OR fhab_is_internal() OR (owner_org = ANY (fhab_user_orgs())))',
            t, t);
    END LOOP;
END $$;

-- Internal-only tables.
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['response','location'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS %I_read ON %I', t, t);
        EXECUTE format(
            'CREATE POLICY %I_read ON %I FOR SELECT USING (fhab_is_admin() OR fhab_is_internal())',
            t, t);
    END LOOP;
END $$;

-- ---------- Write policies (INSERT / UPDATE / DELETE) ----------
-- Each writable table gets a predicate; the loop builds matching insert/update/delete
-- policies. Staff edit within their region; contributors edit only their own org's rows;
-- responses and advisories are staff-only (so contributors submit but cannot self-verify).
DO $$
DECLARE rec record;
BEGIN
    FOR rec IN SELECT * FROM (VALUES
        ('event',    'fhab_is_admin() OR (fhab_is_staff_writer() AND fhab_region_ok(fhab_location_region(location_id))) OR (owner_org = ANY (fhab_user_orgs()))'),
        ('station',  'fhab_is_admin() OR fhab_is_staff_writer() OR (owner_org = ANY (fhab_user_orgs()))'),
        ('sample',   'fhab_is_admin() OR fhab_is_staff_writer() OR (owner_org = ANY (fhab_user_orgs()))'),
        ('result',   'fhab_is_admin() OR fhab_is_staff_writer() OR (owner_org = ANY (fhab_user_orgs()))'),
        ('hab_case', 'fhab_is_admin() OR (fhab_is_staff_writer() AND fhab_region_ok(fhab_waterbody_region(waterbody_id)))'),
        ('waterbody','fhab_is_admin() OR (fhab_is_staff_writer() AND fhab_region_ok(regional_water_board))'),
        ('location', 'fhab_is_admin() OR (fhab_is_staff_writer() AND fhab_region_ok(fhab_waterbody_region(waterbody_id)))'),
        ('response', 'fhab_is_admin() OR fhab_is_staff_writer()'),
        ('advisory', 'fhab_is_admin() OR fhab_is_staff_writer()')
    ) AS t(tbl, pred) LOOP
        EXECUTE format('DROP POLICY IF EXISTS %1$I_ins ON %1$I; DROP POLICY IF EXISTS %1$I_upd ON %1$I; DROP POLICY IF EXISTS %1$I_del ON %1$I;', rec.tbl);
        EXECUTE format('CREATE POLICY %1$I_ins ON %1$I FOR INSERT WITH CHECK (%2$s)', rec.tbl, rec.pred);
        EXECUTE format('CREATE POLICY %1$I_upd ON %1$I FOR UPDATE USING (%2$s) WITH CHECK (%2$s)', rec.tbl, rec.pred);
        EXECUTE format('CREATE POLICY %1$I_del ON %1$I FOR DELETE USING (%2$s)', rec.tbl, rec.pred);
    END LOOP;
END $$;

-- ---------- Grants ----------

GRANT USAGE ON SCHEMA public TO fhab_app;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO fhab_app;
-- Writes are allowed only on the tables that have write policies above.
GRANT INSERT, UPDATE, DELETE ON
    event, station, sample, result, hab_case, waterbody, location, response, advisory TO fhab_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO fhab_app;
