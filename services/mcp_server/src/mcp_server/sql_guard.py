"""AST-based guard for LLM-generated SQL (sqlglot, PostgreSQL dialect).

SECURITY INVARIANTS - never relax these (see CLAUDE.md):
  1. exactly one statement, and it must be a SELECT (incl. WITH / UNION of SELECTs);
  2. no DML/DDL/utility nodes anywhere in the tree (also not in CTEs);
  3. only schema-qualified tables from ALLOWED_SCHEMAS (or CTE names);
  4. no dangerous functions (file access, sleeps, config, dblink, ...);
  5. every reference to core.events needs a time filter on event_datetime that is
     not hidden inside an OR branch;
  6. LIMIT is enforced and capped at MAX_ROWS.
The database role ``ai_reader`` (read-only, 3 s timeout) is the second line of defence.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

MAX_ROWS = 1000
ALLOWED_SCHEMAS = frozenset({"core", "mart", "site"})
TIME_FILTERED_TABLES = frozenset({("core", "events")})
TIME_COLUMN = "event_datetime"

_FORBIDDEN_NODE_NAMES = (
    "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "AlterTable", "TruncateTable",
    "Command", "Copy", "Into", "Lock", "Set", "Transaction", "Commit", "Rollback", "Pragma", "Grant",
    "Revoke", "Use", "Analyze", "Describe", "LoadData", "Refresh", "Cache", "Uncache", "Comment",
)
FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = tuple(
    getattr(exp, n) for n in _FORBIDDEN_NODE_NAMES if isinstance(getattr(exp, n, None), type)
)

FORBIDDEN_FUNCTION_PREFIXES = (
    "pg_", "lo_", "dblink", "query_to_", "table_to_", "cursor_to_", "schema_to_", "database_to_",
    "txid_", "set_config", "current_setting", "nextval", "setval", "currval", "version",
)
_COMPARISONS = (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.EQ, exp.Between)


class SQLGuardError(ValueError):
    """Raised when a query violates the read-only policy."""


def _function_name(node: exp.Expression) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name).lower()
    try:
        return node.sql_name().lower()
    except Exception:  # noqa: BLE001
        return type(node).__name__.lower()


def _has_valid_time_filter(condition: exp.Expression | None) -> bool:
    if condition is None:
        return False
    for column in condition.find_all(exp.Column):
        if column.name.lower() != TIME_COLUMN:
            continue
        node: exp.Expression | None = column
        comparison_found, under_or = False, False
        while node is not None and node is not condition.parent:
            if isinstance(node, _COMPARISONS):
                comparison_found = True
            if isinstance(node, exp.Or | exp.Not):
                under_or = True
            if node is condition:
                break
            node = node.parent
        if comparison_found and not under_or:
            return True
    return False


def _check_time_filter(table: exp.Table) -> None:
    select = table.find_ancestor(exp.Select)
    conditions: list[exp.Expression | None] = []
    if select is not None:
        where = select.args.get("where")
        conditions.append(where.this if where is not None else None)
        join = table.find_ancestor(exp.Join)
        if join is not None and join.args.get("on") is not None:
            conditions.append(join.args["on"])
    if not any(_has_valid_time_filter(c) for c in conditions):
        raise SQLGuardError(
            f"queries on core.events must filter on {TIME_COLUMN} "
            f"(e.g. WHERE {TIME_COLUMN} >= now() - interval '1 day'), not inside OR/NOT"
        )


def _limit_value(node: exp.Expression) -> int | None:
    limit = node.args.get("limit")
    if limit is None:
        return None
    target = limit.args.get("expression") if isinstance(limit, exp.Limit) else limit.args.get("count")
    if isinstance(target, exp.Literal) and target.is_int:
        return int(target.this)
    return -1  # non-literal limit -> replace


def validate_sql(sql: str, max_rows: int = MAX_ROWS) -> str:
    """Validate ``sql`` and return a normalised, LIMIT-capped PostgreSQL query string."""
    if not sql or not sql.strip():
        raise SQLGuardError("empty query")
    try:
        statements = [s for s in sqlglot.parse(sql, dialect="postgres") if s is not None]
    except ParseError as exc:
        raise SQLGuardError(f"could not parse SQL: {exc}") from exc
    if len(statements) != 1:
        raise SQLGuardError("exactly one statement is allowed")
    root = statements[0]

    if not isinstance(root, exp.Select | exp.SetOperation):
        raise SQLGuardError(f"only SELECT statements are allowed (got {root.key.upper()})")

    for node in root.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise SQLGuardError(f"forbidden construct: {type(node).__name__.upper()}")
        if isinstance(node, exp.Select) and node.args.get("locks"):
            raise SQLGuardError("row locking (FOR UPDATE/SHARE) is not allowed")
        if isinstance(node, exp.Func):
            name = _function_name(node)
            if name.startswith(FORBIDDEN_FUNCTION_PREFIXES):
                raise SQLGuardError(f"function not allowed: {name}")

    cte_names = {cte.alias_or_name.lower() for cte in root.find_all(exp.CTE)}
    for table in root.find_all(exp.Table):
        name = (table.name or "").lower()
        if not name:
            continue
        schema = (table.db or "").lower()
        if table.catalog:
            raise SQLGuardError("cross-database references are not allowed")
        if not schema:
            if name in cte_names:
                continue
            raise SQLGuardError(f"table '{name}' must be schema-qualified ({', '.join(sorted(ALLOWED_SCHEMAS))})")
        if schema not in ALLOWED_SCHEMAS:
            raise SQLGuardError(f"schema '{schema}' is not accessible")
        if (schema, name) in TIME_FILTERED_TABLES:
            _check_time_filter(table)

    if isinstance(root, exp.Select):
        current = _limit_value(root)
        if current is None or current < 0 or current > max_rows:
            root = root.limit(max_rows, copy=False)
    else:
        root = exp.select("*").from_(root.subquery("guarded_q")).limit(max_rows)

    return root.sql(dialect="postgres")
