-- Idempotent read grants for everything in mart (re-applied after each `make apply-sql`).
-- Read-only roles must NEVER receive INSERT/UPDATE/DELETE/TRUNCATE.
DO $$
BEGIN
    -- database-level access (repairs existing installations on `make apply-sql`)
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO oee_app, grafana_reader, bi_reader, ai_reader', current_database());
    -- ingest_worker stages COPY batches in a session temp table; readers get no TEMP
    EXECUTE format('GRANT TEMPORARY ON DATABASE %I TO oee_app', current_database());
END
$$;

GRANT USAGE ON SCHEMA mart TO oee_app, grafana_reader, bi_reader, ai_reader;

-- Shift model: written by the site config sync (oee_app), readable for dashboards/AI.
GRANT SELECT, INSERT, UPDATE, DELETE ON core.shift_patterns TO oee_app;
GRANT SELECT ON core.shift_patterns TO grafana_reader, ai_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA mart TO oee_app, grafana_reader, bi_reader, ai_reader;

-- KPI functions callable by the read-only roles (functions are read-only, STABLE).
GRANT USAGE ON SCHEMA mart TO grafana_reader, bi_reader, ai_reader;
GRANT EXECUTE ON FUNCTION mart.planned_seconds(text, timestamptz, timestamptz) TO grafana_reader, bi_reader, ai_reader;
GRANT EXECUTE ON FUNCTION mart.oee_summary(timestamptz, timestamptz, text[]) TO grafana_reader, bi_reader, ai_reader;
