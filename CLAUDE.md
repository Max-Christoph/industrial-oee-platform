# Rules for AI coding agents (CLAUDE.md = AGENTS.md)

These rules are **mandatory** for every AI agent (Claude Code, Codex, Copilot, Cursor, …) working in
this repository. They override convenience. If a task would violate a rule, stop and ask the human.
`AGENTS.md` is an identical copy – change both or neither.

## 1. No secrets, no company data – ever
- NEVER write real server names, IP addresses, hostnames, SAP/material/order numbers, internal process
  IDs, plant or customer names, people's names, passwords, API keys or certificates into tracked files.
- Plant- and IT-specific content belongs **only** in `site_overrides/` (git-ignored) or `.env`
  (git-ignored). Examples in the repo use the fictional demo plant (`examples/demo_plant/`).
- Never commit `.env`, `site_overrides/*` (except its README / `certs/.gitkeep`), `*.key`, `*.pem`, dumps.
- Before every commit: `git status` + `git diff --cached` and check for the above.

## 2. SQL = domain logic, Python = transport
- **All** domain calculations – OEE, availability, performance, quality, cycle-time statistics and
  percentiles, histograms, stop/microstop/downtime classification, START/END pairing – live
  **exclusively** in PostgreSQL/TimescaleDB: views, continuous aggregates, PL/pgSQL functions or
  procedures in `db/mart/`.
- Python services (`ingest_worker`, `api_gateway`, `mcp_server`, `shared`) contain **no** domain
  calculations. They only transport, validate (schema, plausibility, time zone), and serialise data.
  An API endpoint is `SELECT … FROM mart.v_… WHERE <filters> LIMIT …` – nothing more.
- If you need a new number, write SQL first; never compute it in Python, Grafana or the BI tool.

## 3. Schema discipline (`core` vs. `mart`)
- `core` = raw, generic, English, append-only. **Never** alter `core` tables destructively
  (no `DROP`/`RENAME`/type change/`DELETE` of columns or data). Additive, backward-compatible
  changes only (new nullable column, new index) and only when unavoidable.
- New KPI logic and BI queries go **only** into `mart` (views, aggregates, functions) or, for a single
  plant, into `site_overrides/custom_views.sql` (schema `site`).
- Additive schema changes for existing installations go into `db/mart/05_schema_additions.sql`
  (`ADD COLUMN IF NOT EXISTS`, `CREATE TABLE IF NOT EXISTS`) – never drop, rename or retype.
- Every file in `db/mart/` is **idempotent** (`CREATE OR REPLACE VIEW/FUNCTION/PROCEDURE`,
  `CREATE … IF NOT EXISTS`, `GRANT`). It must be safe to re-run at any time with `make apply-sql`
  on a production database without rebuild, restart or data loss. Never put `DROP TABLE`,
  `TRUNCATE` or `DELETE` there. Files run in name order (`10_…`, `20_…`, `90_grants.sql` last).
- Timestamps: always `TIMESTAMPTZ`, always processed and stored in **UTC**. Never `timestamp without
  time zone`, never local time in the database. Time-zone conversion happens only at ingest
  (`SITE_TIMEZONE` → UTC) or at presentation (Grafana/BI).
  Single exception: the shift model `core.shift_patterns` stores wall-clock `time` values together
  with an explicit IANA `timezone` column; they are converted to UTC only in SQL (`mart.planned_seconds`).
- Keep all identifiers, enum values and column names generic English. Localised names only as
  aliases in `site_overrides/custom_views.sql`.

## 4. Security invariants – never weaken
- `services/mcp_server/src/mcp_server/sql_guard.py` (sqlglot AST guard) must never be relaxed:
  single `SELECT` only, no DML/DDL/locking/dangerous functions, schema-qualified `core`/`mart`/`site`
  tables only, `LIMIT` ≤ 1000 enforced, mandatory `event_datetime` filter on `core.events`
  (not inside `OR`/`NOT`). Extending a blocklist is fine; removing checks is not.
  `tests/unit/test_sql_guard.py` must keep passing unchanged (you may add cases, never delete them).
- Read-only roles `grafana_reader`, `bi_reader`, `ai_reader` must **never** receive `INSERT`,
  `UPDATE`, `DELETE`, `TRUNCATE`, `CREATE` or ownership. Allowed: `SELECT`, `USAGE`, `CONNECT` and
  `EXECUTE` on read-only (`STABLE`) functions in `mart`. A `SECURITY DEFINER` function must be
  read-only and pin `SET search_path = pg_catalog, pg_temp`. `ai_reader` keeps
  `default_transaction_read_only = on` and `statement_timeout = 3s`. New mart objects get
  `SELECT` only (via `db/mart/90_grants.sql`).
- API stays read-only and behind `X-API-Key`; no new unauthenticated endpoints besides `/health`.
- Remote PostgreSQL connections stay TLS-only (`hostssl`); MCP stays bound to loopback by default.

## 5. One stack: Python ≥ 3.12 (uv) + SQL + rootless Podman
- Only Python ≥ 3.12 managed with `uv` (`uv add`, `uv sync`, `uv.lock` committed) and SQL. No other
  programming languages, no Node/Go/Java services.
- No heavy middleware: no Kafka, Redis, RabbitMQ, Airflow, separate scheduler or ORM container.
- Machine connectivity (Node-RED, PLC drivers) runs on an edge gateway and is documented in
  `docs/connectivity.md` / `examples/edge/` – it is not part of the core stack.
  Scheduling = TimescaleDB jobs. Prefer fewer containers.
- `docker-compose.yml` must work with rootless Podman: every new bind mount has an SELinux flag
  (`:ro,z` read-only, `:z` if writable), every new service has a `healthcheck`, `restart`, the shared
  `logging` anchor and `networks: [oee_net]`. Pin image versions; use fully qualified image names.

## 6. Definition of Done (before every commit)
`make verify` runs steps 1–3 (except the apply-sql check, which needs a running stack).
1. `make test` passes (unit tests; integration tests with `OEE_TEST_DSN` when SQL changed).
2. `make lint` passes.
3. Schema or compose changes: `podman compose config` renders without errors; SQL in `db/mart/`
   applied twice in a row without error (`make apply-sql`).
4. New behaviour has a pytest test (KPI SQL → integration test, endpoint → `tests/unit/test_api.py`).
5. README / `site_overrides/README.md` updated if a workflow changed.
6. `git status` clean of secrets and site data; conventional commit message (`feat:`, `fix:`, …).

## Map of the repo
| Path | Purpose |
|------|---------|
| `db/init/` | first-start only: roles, `core` + `mart` tables, job registration, grants, pg_hba |
| `db/mart/` | idempotent views/functions/procedures/grants → `make apply-sql` |
| `services/shared` | `TelemetryEvent` schema, site config loader (transport/validation only) |
| `services/ingest_worker` | MQTT → COPY batches → `core.events` |
| `services/api_gateway` | read-only REST on `mart.v_*` |
| `services/mcp_server` | MCP tools `get_schema`, `run_sql` (+ `sql_guard.py`) |
| `site_overrides/` | plant/IT specifics (git-ignored) |
| `.claude/commands/` | guided workflows: `/add-kpi-view`, `/configure-site` |
