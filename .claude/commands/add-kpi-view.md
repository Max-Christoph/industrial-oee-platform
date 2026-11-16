---
description: Guided workflow - add a new KPI as idempotent SQL view in db/mart/, a read-only API endpoint, grants and tests
argument-hint: "[short description of the KPI, e.g. 'scrap rate per shift']"
---

# /add-kpi-view – add a new KPI safely

You are guiding a colleague who may have little SQL/Python experience. Be friendly, explain each step
in one sentence, and **follow CLAUDE.md strictly** (SQL = domain logic, Python = transport; `core` is
never changed destructively; read-only roles get `SELECT` only; no secrets / plant data in the repo).

KPI request from the user: $ARGUMENTS

## Step 0 – Understand the KPI (ask, don't guess)
If anything below is unclear, ask with **one compact question block** and wait for the answer:
1. What should the number express? (e.g. "share of microstop time per hour")
2. Granularity: per hour / day / shift? Per asset (machine), line or plant?
3. Which existing data does it come from? Map it to existing objects – read them first:
   `db/mart/10_bi_views.sql`, `db/init/20_mart.sql`, `db/init/10_core.sql`.
   Prefer `mart.event_intervals`, `mart.cycle_metrics`, `mart.v_*` over raw `core.events`.
4. A short **English snake_case name**, e.g. `microstop_share_hourly` → view `mart.v_microstop_share_hourly`.

If the KPI needs data that does not exist (e.g. scrap counts), stop and explain what events would be
needed – do not invent columns and do not alter `core` tables.

Show the user a 3-line plan (view name, source objects, formula) and get a "yes" before writing files.

## Step 1 – SQL view in `db/mart/` (the only place for the calculation)
Create a new file `db/mart/<NN>_<kpi_name>.sql` with the next free number between `30` and `89`
(check `ls db/mart/`). Template:

```sql
-- KPI: <one-line business meaning>
-- Grain: <asset_id x hour|day> ; Source: <mart objects>
-- Idempotent: safe to re-apply any time via `make apply-sql`.
CREATE OR REPLACE VIEW mart.v_<kpi_name> AS
SELECT
    m.bucket,                         -- timestamptz, UTC
    m.asset_id,
    h.name AS asset_name,
    h.path AS asset_path,
    <formula> AS <kpi_column>         -- guard divisions: NULLIF(denominator, 0)
FROM mart.cycle_metrics m
LEFT JOIN mart.v_asset_hierarchy h USING (asset_id);

COMMENT ON VIEW mart.v_<kpi_name> IS '<business meaning, unit, grain>';
```

Rules:
- Only `CREATE OR REPLACE VIEW` / `CREATE OR REPLACE FUNCTION` / `COMMENT` / `GRANT`. Never `DROP`,
  `TRUNCATE`, `DELETE`, `ALTER TABLE core.*`.
- `CREATE OR REPLACE VIEW` cannot remove/rename existing columns – only append new columns at the end.
- Time columns stay `timestamptz` in UTC; ratios as `double precision` 0..1 (document unit in the comment).
- Always handle division by zero with `NULLIF(..., 0)`.

## Step 2 – Grants (read-only)
`db/mart/90_grants.sql` already grants `SELECT ON ALL TABLES IN SCHEMA mart` (views included) to
`grafana_reader`, `bi_reader`, `ai_reader` and runs last on every `make apply-sql`. Add an explicit
line only for documentation if you like:

```sql
GRANT SELECT ON mart.v_<kpi_name> TO grafana_reader, bi_reader, ai_reader;
```
Never grant anything except `SELECT` to these roles.

## Step 3 – Activate it
If the stack is running: `make apply-sql` (no rebuild, no restart, no data loss). Run it **twice** –
the second run must also succeed (proves idempotency). If it is not running, tell the user the view
is applied automatically on the next first-time init and via `make apply-sql` afterwards.

## Step 4 – Read-only API endpoint (transport only)
In `services/api_gateway/src/api_gateway/app.py` add, next to the other `/v1/...` endpoints:

```python
    @app.get("/v1/<kpi-name-with-dashes>", tags=["kpi"], dependencies=v1)
    async def <kpi_name>(
        db: DB,
        asset_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 1000,
    ) -> list[dict[str, Any]]:
        start, end = _window(start, end, timedelta(days=1))
        return await db.fetch(
            "SELECT * FROM mart.v_<kpi_name> WHERE bucket >= %(start)s AND bucket < %(end)s "
            "AND (%(asset_id)s::text IS NULL OR asset_id = %(asset_id)s) "
            "ORDER BY bucket, asset_id LIMIT %(limit)s",
            {"start": start, "end": end, "asset_id": asset_id, "limit": limit},
        )
```
- No calculations in Python, only parameterised `SELECT` from the view (never f-string user input).
- Keep `dependencies=v1` (X-API-Key).

## Step 5 – Tests (mandatory)
1. `tests/unit/test_api.py`: add the new path to `ENDPOINTS` (auth tests) and add a test that the
   endpoint queries `mart.v_<kpi_name>` with parameters (copy `test_cycle_metrics_queries_only_mart_with_params`).
2. `tests/integration/test_pipeline_db.py`: add a test that inserts a few events with `write(...)`,
   calls `refresh(conn)`, then `SELECT`s from `mart.v_<kpi_name>` and asserts the expected value
   computed by hand in the test.

## Step 6 – Verify (Definition of Done)
```bash
make lint
make test
```
If a database is available: `OEE_TEST_DSN=... make test` so the integration test runs. Fix failures –
never weaken or delete existing tests. Then show the user: the new file, the endpoint URL, how to use
it in Grafana/Power BI (`mart.v_<kpi_name>`), and propose a commit message
`feat(mart): add <kpi_name> KPI view and endpoint`. Do not commit unless the user says so.

## If something goes wrong
- `make apply-sql` error "cannot change name of view column": you renamed/removed a column → create a
  new view name (`..._v2`) instead of changing the old one.
- "permission denied for …": the view reads a table the reader role cannot access → that is fine for
  views (they run with owner rights); check you are not granting on base tables.
- Unsure whether something is domain logic? It is. Put it in SQL.
