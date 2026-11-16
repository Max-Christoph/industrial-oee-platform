-- =============================================================================
-- Least-privilege grants
--   oee_app        : ingest writer (events, error_logs, master data sync, site views)
--   grafana_reader : dashboards (core + mart + site, read-only)
--   bi_reader      : Power BI / corporate BI and api_gateway (mart + site, read-only)
--   ai_reader      : MCP / LLM access (core + mart + site, read-only, 3 s timeout)
-- =============================================================================
DO $$
DECLARE
    v_db text := current_database();
BEGIN
    EXECUTE format('REVOKE ALL ON DATABASE %I FROM PUBLIC', v_db);
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO oee_app, grafana_reader, bi_reader, ai_reader', v_db);
    -- ingest_worker stages COPY batches in a session temp table
    EXECUTE format('GRANT TEMPORARY ON DATABASE %I TO oee_app', v_db);
END
$$;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- Writer -----------------------------------------------------------------------
GRANT USAGE ON SCHEMA core, mart TO oee_app;
GRANT USAGE, CREATE ON SCHEMA site TO oee_app;
GRANT SELECT, INSERT ON core.events, core.error_logs TO oee_app;
GRANT SELECT, INSERT, UPDATE ON core.assets TO oee_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON core.oee_rules TO oee_app;
GRANT SELECT ON ALL TABLES IN SCHEMA mart TO oee_app;   -- custom site views build on mart

-- Readers ----------------------------------------------------------------------
GRANT USAGE ON SCHEMA core, mart, site TO grafana_reader, ai_reader;
GRANT USAGE ON SCHEMA mart, site TO bi_reader;

GRANT SELECT ON ALL TABLES IN SCHEMA core TO grafana_reader, ai_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA mart TO grafana_reader, bi_reader, ai_reader;

-- Future objects ---------------------------------------------------------------
ALTER DEFAULT PRIVILEGES IN SCHEMA mart GRANT SELECT ON TABLES TO oee_app, grafana_reader, bi_reader, ai_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA core GRANT SELECT ON TABLES TO grafana_reader, ai_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE oee_app IN SCHEMA site
    GRANT SELECT ON TABLES TO grafana_reader, bi_reader, ai_reader;
