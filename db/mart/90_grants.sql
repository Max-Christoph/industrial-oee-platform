-- Idempotent read grants for everything in mart (re-applied after each `make apply-sql`).
-- Read-only roles must NEVER receive INSERT/UPDATE/DELETE/TRUNCATE.
GRANT USAGE ON SCHEMA mart TO oee_app, grafana_reader, bi_reader, ai_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA mart TO oee_app, grafana_reader, bi_reader, ai_reader;
