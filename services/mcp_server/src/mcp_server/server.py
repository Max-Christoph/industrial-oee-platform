"""FastMCP server exposing the OEE data model to LLM agents (read-only).

Tools:
  * ``get_schema``  - tables/views/columns of core, mart and site + usage hints
  * ``run_sql``     - AST-guarded SELECT (see sql_guard.py), executed as ai_reader

Transports: streamable-http (default, for containers) or ``--stdio`` (local agents).
"""

from __future__ import annotations

import argparse
import datetime as dt
import decimal
import logging
import os
import uuid
from functools import lru_cache
from typing import Any

from fastmcp import FastMCP

from mcp_server.sql_guard import ALLOWED_SCHEMAS, MAX_ROWS, SQLGuardError, validate_sql

log = logging.getLogger("mcp_server")

INSTRUCTIONS = f"""Read-only access to an industrial OEE / cycle-time database (PostgreSQL + TimescaleDB).
All timestamps are TIMESTAMPTZ in UTC.
Prefer the curated views in schema `mart` (v_cycle_metrics_hourly, v_cycle_metrics_daily,
v_cycle_histogram, v_stop_events, v_asset_hierarchy, v_pipeline_health).
Raw events live in core.events and MUST be filtered on event_datetime.
Only single SELECT statements are accepted; results are capped at {MAX_ROWS} rows.
Call get_schema first."""

SCHEMA_SQL = """
SELECT c.table_schema, c.table_name, t.table_type, c.column_name, c.data_type
FROM information_schema.columns c
JOIN information_schema.tables t USING (table_schema, table_name)
WHERE c.table_schema = ANY(%s)
ORDER BY c.table_schema, c.table_name, c.ordinal_position
"""


@lru_cache(maxsize=1)
def _pool() -> Any:
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    dsn = os.environ.get("DATABASE_URL", "postgresql://ai_reader@localhost:5432/oee?sslmode=prefer")
    return ConnectionPool(
        dsn,
        min_size=0,
        max_size=4,
        open=True,
        kwargs={"row_factory": dict_row, "autocommit": False, "application_name": "oee-mcp-server"},
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, uuid.UUID | bytes | memoryview):
        return str(value)
    return value


def _query(sql: str, params: tuple[Any, ...] | None = None) -> list[dict[str, Any]]:
    with _pool().connection() as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        conn.rollback()
    return [{k: _json_safe(v) for k, v in row.items()} for row in rows]


def get_schema() -> dict[str, Any]:
    """Return all queryable tables/views with their columns plus usage rules."""
    tables: dict[str, dict[str, Any]] = {}
    for row in _query(SCHEMA_SQL, (sorted(ALLOWED_SCHEMAS),)):
        key = f"{row['table_schema']}.{row['table_name']}"
        entry = tables.setdefault(key, {"type": row["table_type"], "columns": {}})
        entry["columns"][row["column_name"]] = row["data_type"]
    return {
        "rules": [
            "single SELECT only, max 1000 rows",
            "core.events requires a filter on event_datetime",
            "all timestamps are timestamptz (UTC)",
        ],
        "tables": tables,
    }


def run_sql(query: str) -> dict[str, Any]:
    """Run a single read-only SELECT. core.events needs an event_datetime filter; LIMIT <= 1000."""
    try:
        safe_sql = validate_sql(query)
    except SQLGuardError as exc:
        return {"error": str(exc)}
    try:
        rows = _query(safe_sql)
    except Exception as exc:  # noqa: BLE001 - report DB errors (timeouts etc.) to the agent
        return {"error": f"database error: {str(exc).strip()}", "executed_sql": safe_sql}
    return {"executed_sql": safe_sql, "row_count": len(rows), "rows": rows}


def build_server() -> FastMCP:
    mcp = FastMCP("industrial-oee-platform", instructions=INSTRUCTIONS)
    mcp.tool(get_schema, annotations={"readOnlyHint": True})
    mcp.tool(run_sql, annotations={"readOnlyHint": True})
    return mcp


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="industrial-oee-platform MCP server")
    parser.add_argument("--stdio", action="store_true", help="use stdio transport (local agents)")
    parser.add_argument("--host", default=os.environ.get("MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", "8001")))
    args = parser.parse_args(argv)

    from oee_shared.logging_setup import setup_logging

    setup_logging("mcp_server")
    server = build_server()
    if args.stdio:
        server.run(transport="stdio", show_banner=False)
    else:
        server.run(transport="streamable-http", host=args.host, port=args.port, show_banner=False)
