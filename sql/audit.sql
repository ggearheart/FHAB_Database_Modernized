-- Row-level audit of in-place changes (governance review #5). Append-only history of who
-- changed what, when, and the before/after — for the high-value mutable tables that feed the
-- authoritative public dataset. UPDATE + DELETE only: INSERTs are intentionally NOT audited
-- (bulk ingest would swamp the log, and creation is already evident from the row + created_at).
--
-- Actor = current_setting('fhab.user_id'), which the web layer sets per request (both the
-- acting_as/RLS path and the owner path). NULL actor = a system write (loader, import, refresh,
-- migration) with no logged-in user — itself meaningful (e.g. a data.ca.gov refresh overwrite).
--
-- Idempotent: safe to re-run on every boot (apply_schema).

CREATE TABLE IF NOT EXISTS audit_log (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at         timestamptz NOT NULL DEFAULT now(),
    actor_id   bigint,                 -- app_user.id, or NULL for system/import writes
    table_name text NOT NULL,
    row_key    text,                   -- the row's business key (bloom_report_id, case_id, …)
    action     text NOT NULL,          -- 'UPDATE' | 'DELETE'
    changed    text[],                 -- columns whose value changed (UPDATE only)
    before     jsonb,                  -- the row before (OLD)
    after      jsonb                   -- the row after (NEW); NULL for DELETE
);
CREATE INDEX IF NOT EXISTS audit_log_table_row_idx ON audit_log (table_name, row_key);
CREATE INDEX IF NOT EXISTS audit_log_at_idx        ON audit_log (at DESC);
CREATE INDEX IF NOT EXISTS audit_log_actor_idx     ON audit_log (actor_id);

-- SECURITY DEFINER so the insert into audit_log always succeeds as the table owner, whichever
-- role performed the audited write (owner or fhab_app under acting_as).
CREATE OR REPLACE FUNCTION audit_row_change() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
    uid text  := current_setting('fhab.user_id', true);
    b   jsonb := to_jsonb(OLD);
    a   jsonb := CASE WHEN TG_OP <> 'DELETE' THEN to_jsonb(NEW) END;
    ch  text[];
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF b IS NOT DISTINCT FROM a THEN
            RETURN NEW;                 -- no-op update (e.g. a refresh writing identical values)
        END IF;
        SELECT array_agg(e.key ORDER BY e.key) INTO ch
        FROM jsonb_each(a) e
        WHERE e.value IS DISTINCT FROM (b -> e.key);
    END IF;
    INSERT INTO audit_log (actor_id, table_name, row_key, action, changed, before, after)
    VALUES (nullif(uid, '')::bigint, TG_TABLE_NAME,
            coalesce(a ->> TG_ARGV[0], b ->> TG_ARGV[0]), TG_OP, ch, b, a);
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;

-- Attach (idempotently) to the audited tables, passing each table's business key as the arg.
DO $$
DECLARE t record;
BEGIN
    FOR t IN SELECT * FROM (VALUES
        ('event',    'bloom_report_id'),
        ('hab_case', 'case_id'),
        ('response', 'response_action_id'),
        ('advisory', 'advisory_id'),
        ('sample',   'id')
    ) AS v(tbl, pk) LOOP
        IF to_regclass('public.' || t.tbl) IS NOT NULL THEN
            EXECUTE format('DROP TRIGGER IF EXISTS audit_%1$s ON public.%1$I', t.tbl);
            EXECUTE format(
                'CREATE TRIGGER audit_%1$s AFTER UPDATE OR DELETE ON public.%1$I '
                'FOR EACH ROW EXECUTE FUNCTION audit_row_change(%2$L)', t.tbl, t.pk);
        END IF;
    END LOOP;
END $$;
