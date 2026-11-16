-- Cluster-wide roles. Created NOLOGIN here (idempotent, usable from tests);
-- 06_role_passwords.sh enables LOGIN and sets passwords from the environment.
DO $$
DECLARE
  r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['oee_app', 'grafana_reader', 'bi_reader', 'ai_reader'] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('CREATE ROLE %I NOLOGIN', r);
    END IF;
  END LOOP;
END
$$;

-- AI / LLM access: hard read-only + short timeout as defense in depth behind the
-- sqlglot AST guard in mcp_server.
ALTER ROLE ai_reader SET default_transaction_read_only = on;
ALTER ROLE ai_reader SET statement_timeout = '3s';
ALTER ROLE ai_reader SET idle_in_transaction_session_timeout = '30s';

ALTER ROLE grafana_reader SET default_transaction_read_only = on;
ALTER ROLE grafana_reader SET statement_timeout = '30s';

ALTER ROLE bi_reader SET default_transaction_read_only = on;
ALTER ROLE bi_reader SET statement_timeout = '300s';

ALTER ROLE oee_app SET statement_timeout = '60s';
