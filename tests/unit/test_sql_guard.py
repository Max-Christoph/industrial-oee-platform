"""sqlglot AST guard of the MCP server - security invariants (never relax)."""

from __future__ import annotations

import pytest

from mcp_server.sql_guard import MAX_ROWS, SQLGuardError, validate_sql

ALLOWED = [
    "SELECT * FROM mart.v_cycle_metrics_hourly",
    "select asset_id, avg(cycle_median_s) from mart.cycle_metrics group by 1",
    "SELECT count(*) FROM core.events WHERE event_datetime >= now() - interval '1 day'",
    "SELECT * FROM core.events e WHERE e.event_datetime BETWEEN '2026-01-01' AND '2026-01-02' AND e.asset_id = 'X'",
    "WITH x AS (SELECT * FROM mart.v_stop_events) SELECT * FROM x",
    "SELECT a.name, m.cycle_count FROM core.assets a JOIN mart.cycle_metrics m USING (asset_id)",
    "SELECT * FROM mart.cycle_metrics m JOIN core.events e ON e.asset_id = m.asset_id "
    "AND e.event_datetime > now() - interval '1 hour'",
    "SELECT * FROM site.cycle_kpis_daily",
    "SELECT * FROM mart.a UNION ALL SELECT * FROM mart.b",
]

BLOCKED = {
    "dml_insert": "INSERT INTO core.events VALUES (1)",
    "dml_update": "UPDATE core.assets SET name = 'x'",
    "dml_delete": "DELETE FROM core.events",
    "ddl_drop": "DROP TABLE core.events",
    "ddl_create": "CREATE TABLE mart.x (a int)",
    "truncate": "TRUNCATE core.events",
    "select_into": "SELECT * INTO mart.copy FROM mart.cycle_metrics",
    "multi_statement": "SELECT 1; DROP TABLE core.events",
    "cte_dml": "WITH d AS (DELETE FROM core.assets RETURNING *) SELECT * FROM d",
    "for_update": "SELECT * FROM mart.cycle_metrics FOR UPDATE",
    "pg_sleep": "SELECT pg_sleep(10)",
    "read_file": "SELECT pg_read_file('/etc/passwd')",
    "set_config": "SELECT set_config('statement_timeout', '0', false)",
    "dblink": "SELECT * FROM dblink('host=x', 'select 1') AS t(a int)",
    "query_to_xml": "SELECT query_to_xml('delete from core.events', true, true, '')",
    "catalog": "SELECT * FROM pg_catalog.pg_authid",
    "info_schema": "SELECT * FROM information_schema.tables",
    "public_schema": "SELECT * FROM public.secrets",
    "unqualified": "SELECT * FROM events",
    "no_time_filter": "SELECT * FROM core.events",
    "time_filter_other_column": "SELECT * FROM core.events WHERE received_at > now() - interval '1 day'",
    "time_filter_or_bypass": "SELECT * FROM core.events WHERE event_datetime > now() - interval '1 day' OR true",
    "time_filter_not": "SELECT * FROM core.events WHERE NOT (event_datetime < now())",
    "subquery_without_filter": "SELECT * FROM mart.cycle_metrics WHERE asset_id IN (SELECT asset_id FROM core.events)",
    "set_statement": "SET statement_timeout = 0",
    "copy": "COPY core.events TO '/tmp/x'",
    "empty": "   ",
    "garbage": "SELEKT * FROM",
    "nextval": "SELECT nextval('x')",
}


@pytest.mark.parametrize("sql", ALLOWED)
def test_allowed(sql):
    assert validate_sql(sql).upper().startswith(("SELECT", "WITH"))


@pytest.mark.parametrize("name", sorted(BLOCKED))
def test_blocked(name):
    with pytest.raises(SQLGuardError):
        validate_sql(BLOCKED[name])


def test_limit_added_when_missing():
    assert validate_sql("SELECT * FROM mart.cycle_metrics").endswith(f"LIMIT {MAX_ROWS}")


def test_limit_capped():
    out = validate_sql("SELECT * FROM mart.cycle_metrics LIMIT 999999")
    assert out.endswith(f"LIMIT {MAX_ROWS}") and "999999" not in out


def test_small_limit_kept():
    assert validate_sql("SELECT * FROM mart.cycle_metrics LIMIT 5").endswith("LIMIT 5")


def test_non_literal_limit_replaced():
    out = validate_sql("SELECT * FROM mart.cycle_metrics LIMIT (SELECT 100000)")
    assert out.endswith(f"LIMIT {MAX_ROWS}")


def test_union_wrapped_and_limited():
    out = validate_sql("SELECT asset_id FROM mart.a UNION SELECT asset_id FROM mart.b")
    assert out.startswith("SELECT * FROM (") and out.endswith(f"LIMIT {MAX_ROWS}")


def test_output_is_reparseable_and_still_safe():
    first = validate_sql("SELECT * FROM core.events WHERE event_datetime > now() - interval '2 hours'")
    assert validate_sql(first) == first
