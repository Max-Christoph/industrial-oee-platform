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
GRANT SELECT ON ALL TABLES IN SCHEMA mart TO oee_app, grafana_reader, bi_reader, ai_reader;
