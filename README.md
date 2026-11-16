# industrial-oee-platform

A lean, production-ready **on-premise IIoT & OEE data platform**: machine events arrive via MQTT,
are stored in PostgreSQL/TimescaleDB and turned into cycle-time and downtime analytics for Grafana,
Power BI / corporate BI, a REST API and LLM agents (MCP).

It runs **stand-alone without an IT department** (one `make up`, self-signed TLS) and also
**inside strict corporate networks** (your own PKI certificates, proxies, air-gapped image
transfer, SSO, read-only BI roles).

```
 PLC / gateway ──MQTT/TLS:8883──▶ Mosquitto ──QoS1──▶ ingest_worker ──COPY──▶ TimescaleDB ◀── Grafana :3000
                                                     (validate, batch)        │  core.*   ◀── Power BI (SSL, bi_reader)
                                                                              │  mart.*   ◀── api_gateway :8000 (X-API-Key)
                                       native TimescaleDB job (every 1 min) ──┘  site.*   ◀── mcp_server :8001 (ai_reader)
                                       START/END pairing, cycle stats, stops
```

**6 containers, nothing else:** `timescaledb`, `mosquitto`, `ingest_worker`, `api_gateway`,
`mcp_server`, `grafana`. No Kafka, no Redis, no scheduler container – aggregation runs as a
TimescaleDB background job.

| Target | Value |
|--------|-------|
| Sustained ingest | ≥ 100 events/s (benchmarked 100 / 250 / 500 ev/s, 0 % loss) |
| Event model | START/END pairs joined via `event_uid`, plus INSTANT events |
| Categories | `PROCESS`, `ERROR`, `SETUP`, `STANDBY`, `INFO`, `WARNING` |
| Analytics | cycle time median/avg/min/max/P25/P75/P95, histogram, microstops, downtimes, setup |

---

## Quick start

Requirements: Linux host with **rootless Podman ≥ 4.9** + `podman compose` (podman-compose or
docker-compose provider), `make`, `openssl`; for tests `uv`.

```bash
make doctor                           # checks podman, compose, uv, linger, ports, .env, certs + prints fixes
sudo loginctl enable-linger "$USER"   # once (doctor tells you): keep rootless containers running after logout
make up                               # preflight, .env with random secrets, TLS certs, build + start
make ps                               # all services should become "healthy"
```

| Service | URL | Credentials |
|---------|-----|-------------|
| Grafana dashboard "OEE Live" | http://localhost:3000 | `GRAFANA_ADMIN_*` in `.env` |
| REST API (OpenAPI) | http://localhost:8000/docs | header `X-API-Key: <API_KEYS from .env>` |
| MCP (streamable HTTP) | http://127.0.0.1:8001/mcp | loopback only by default |
| MQTT (TLS) | `mqtts://<host>:8883` | `MQTT_DEVICE_USER` / `MQTT_DEVICE_PASSWORD`, CA: `site_overrides/certs/ca.crt` |
| PostgreSQL (TLS enforced) | `<host>:5432`, db `oee` | `bi_reader` / `BI_DB_PASSWORD` |

### All workflows

| Command | What it does |
|---------|--------------|
| `make doctor` | host check with copy-paste fixes (start here if anything fails) |
| `make up` / `make down` | start (build) / stop the stack – volumes are kept |
| `make dev [SVC=api\|ingest\|mcp]` | DB + MQTT in containers, Python services locally with hot reload |
| `make verify` | Definition of Done: lint + tests + `compose config` – run before every commit |
| `make logs [S=ingest_worker]` | follow logs |
| `make test` | `uv sync` + full pytest suite (integration tests need `OEE_TEST_DSN`) |
| `make bench` | sustained-ingest benchmark 100/250/500 ev/s: p50/p95/p99 latency, throughput, loss |
| `make backup` / `make restore FILE=…` | `pg_dump -Fc` into `./backups` (rotating) / restore |
| `make apply-sql` | re-apply idempotent `db/mart/*.sql` to the running DB (no rebuild, no data loss) |
| `make apply-site` | re-sync `site_overrides/` (rules, assets, custom views) |
| `make export-images` / `make import-images` | air-gap: save all images to `dist/oee-images.tar` / load them |
| `make reset` | **delete all data volumes** (asks for confirmation) |
| `make help` | everything else (`psql`, `config`, `lint`, …) |

---

## Sending events

Topic: `oee/<site>/<asset_id>/events` (asset id may also be in the payload). Payload: one JSON
object or an array of objects.

```json
[
  {"event_uid": "M01-000123", "asset_id": "LINE-01-M01", "event_state": "START",
   "event_category": "PROCESS", "event_datetime": "2026-11-16T08:15:00.120Z"},
  {"event_uid": "M01-000123", "asset_id": "LINE-01-M01", "event_state": "END",
   "event_category": "PROCESS", "event_datetime": "2026-11-16T08:15:12.480Z", "cycle_time_s": 12.36}
]
```

* `event_datetime`: ISO-8601 or epoch s/ms. **Naive timestamps are interpreted in `SITE_TIMEZONE`**
  and converted to UTC; everything is stored as `timestamptz` (UTC).
* Unknown fields are kept in `attributes` (jsonb).
* Rejected input (invalid JSON, negative/implausible cycle times, timestamps > 5 min in the future,
  unknown categories) is written to `core.error_logs` – valid neighbours in the same message/batch
  are still stored.
* Re-sent events are ignored (idempotency key `(event_datetime, event_uid, event_state)`).

```bash
mosquitto_pub -h <host> -p 8883 --cafile site_overrides/certs/ca.crt -u oee_device -P '<pw>' -q 1 \
  -t oee/demo/LINE-01-M01/events -m '{"event_uid":"t1","event_state":"START","event_category":"PROCESS","event_datetime":"2026-11-16T08:00:00Z"}'
```

---

## Data model

| Object | Description |
|--------|-------------|
| `core.assets` | asset hierarchy via `parent_id` (site → area → line → machine) |
| `core.oee_rules` | analysis thresholds per asset (`*` = plant default), synced from `oee_rules.yaml` |
| `core.events` | hypertable, 1-day chunks, compression after 7 days (buffer for late offline events) |
| `core.error_logs` | hypertable, 90-day retention: rejected payloads and job errors |
| `mart.event_intervals` | paired START/END intervals with `stop_class` (`CYCLE`, `MICROSTOP`, `DOWNTIME`, `SETUP`) and validity |
| `mart.cycle_metrics` | hourly per asset: median, avg, min, max, P25, P75, P95, microstops, downtimes, setup |
| `mart.cycle_histogram` | daily cycle-time distribution per asset |
| `mart.v_*` | **stable BI contract**: `v_asset_hierarchy`, `v_cycle_metrics_hourly`, `v_cycle_metrics_daily`, `v_cycle_histogram`, `v_stop_events`, `v_pipeline_health`, `v_error_logs` |
| `site.*` | optional site-specific alias views (`custom_views.sql`) |

**Aggregation:** the procedure `mart.refresh_cycle_metrics` (in `db/mart/`) runs every minute as a
native TimescaleDB job (`add_job`). It processes only events received since the last watermark
(with a 5-minute safety overlap), pairs them with partners up to 7 days back, recomputes the
affected hours/days and logs any failure to `core.error_logs` (`EXCEPTION` block) instead of
crashing. Classification: `PROCESS` → cycle (valid if `min_cycle_s ≤ d ≤ max_cycle_s`),
`ERROR`/`STANDBY` → microstop if `< microstop_max_s`, otherwise downtime, `SETUP` → setup.

**Roles (least privilege):**

| Role | Access | Used by |
|------|--------|---------|
| `oee_app` | INSERT events/error_logs, sync assets/rules, CREATE in `site` | ingest_worker |
| `grafana_reader` | SELECT core, mart, site (read-only) | Grafana |
| `bi_reader` | SELECT mart, site (read-only) | Power BI, api_gateway |
| `ai_reader` | SELECT core, mart, site; `default_transaction_read_only=on`, `statement_timeout=3s` | mcp_server |

---

## Adapting to your plant and IT (without touching core code)

Everything site-specific goes into **`site_overrides/`** (git-ignored); each file overrides its
counterpart from the fictional demo plant in `examples/demo_plant/`:

* `oee_rules.yaml` – thresholds per asset + payload mapping (field/category/state/asset aliases,
  asset-from-topic regex) to translate PLC dialects into the generic schema.
* `assets.csv` – your asset hierarchy.
* `custom_views.sql` – BI alias views in schema `site` (e.g. localized column names for Power BI).
* `certs/server.crt`, `certs/server.key` (+ `certs/ca.crt`) – your **corporate PKI** certificates
  for PostgreSQL and Mosquitto. If none are present, `make up` creates a self-signed CA + server
  certificate valid for 10 years (extra SANs via `CERT_HOSTNAMES` in `.env`).

Apply changes with `make apply-site`. See [site_overrides/README.md](site_overrides/README.md).

### Corporate networks

* **Proxy:** set `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` in `.env` – passed as build args.
* **Internal registry:** override `TIMESCALEDB_IMAGE`, `MOSQUITTO_IMAGE`, `GRAFANA_IMAGE`, `PYTHON_IMAGE`.
* **Air gap:** `make export-images` on a connected host, copy `dist/oee-images.tar`,
  `make import-images && make up` on the target.
* **Power BI:** PostgreSQL connector, server `<host>:5432`, database `oee`, user `bi_reader`,
  encrypted connection on (TLS is enforced for all remote connections; clients must trust
  `ca.crt` or your PKI). Use the `mart.v_*` views.
* **SSO for Grafana:** fill in the `GF_AUTH_GENERIC_OAUTH_*` (OIDC) or `GF_AUTH_PROXY_*` variables in `.env`.
* **Ports / exposure:** every published port has a `*_PUBLISH_ADDRESS` (MCP defaults to `127.0.0.1`).

### 24/7 operation

Container log rotation (`json-file`, 20 MB × 5), `restart: unless-stopped`, healthchecks for all
services, PostgreSQL defaults `shared_buffers=1GB`, `max_wal_size=2GB`, Mosquitto
`max_queued_messages 50000` and `persistent_client_expiration 7d` (persistent QoS 1 session for
the ingest worker – the broker buffers while the worker or the DB is down), reconnect backoff for
MQTT and DB, batches are retried until stored.

> Podman note (verified with Podman 5.8): `max-size: 20m` is applied, `max-file` is accepted but
> ignored by Podman (Docker honours it). For a hard cap on Podman hosts additionally set
> `log_size_max` in `containers.conf`, or use `journald` with systemd's own retention.

### Windows development hosts (Podman machine / WSL2)

The stack runs unchanged on Windows with `podman machine` (verified: all 6 services healthy,
integration tests and benchmark green). Use **Git-Bash**; `make` is optional
(`winget install -e --id ezwinports.make`), every target is a thin wrapper around `scripts/*.sh`
and `podman compose …`. `make doctor` detects Windows, checks the Podman machine and prints the
`winget`/`podman machine` fixes. The Makefile sets `MSYS_NO_PATHCONV=1` on Windows so container
paths like `/app/scripts/benchmark.py` are not rewritten. The SELinux flags (`:z`, `:ro,z`) are
harmless there (no SELinux in the WSL VM); `loginctl enable-linger` only matters on Linux
production hosts. Give the machine ≥ 2 GiB RAM (`PG_SHARED_BUFFERS=1GB` default).

---

## MCP server (LLM access)

Tools: `get_schema` and `run_sql`. Every query is parsed with **sqlglot** and must be a single
`SELECT` (no DML/DDL/locking/dangerous functions, schema-qualified `core`/`mart`/`site` tables
only); `LIMIT` is enforced (≤ 1000) and queries on `core.events` must filter on
`event_datetime`. The DB role `ai_reader` is read-only with a 3 s timeout as a second line of defence.

```bash
uv run python -m mcp_server --stdio            # local agent (DATABASE_URL=postgresql://ai_reader:...)
```

---

## Development

**House rules** (binding for humans and AI agents – see [CLAUDE.md](CLAUDE.md) / [AGENTS.md](AGENTS.md)):
**SQL = domain logic, Python = transport.** Every KPI, percentile or stop classification is SQL in
`db/mart/`; the Python services only validate, move and serialise data. `core` is never changed
destructively, read-only roles only ever get `SELECT`, the MCP SQL guard is never relaxed, no plant
data or secrets outside `site_overrides/` / `.env`, one stack (Python ≥ 3.12 + uv, SQL, rootless Podman).

```bash
uv sync --all-packages        # Python ≥ 3.12 workspace (services/*)
make test                     # unit + guardrail tests (no infrastructure needed)
OEE_TEST_DSN='postgresql://postgres:<pw>@localhost:5432/postgres?sslmode=require' make test
                              # + integration tests: builds a throw-away DB from db/init + db/mart
make verify                   # lint + tests + compose config = Definition of Done
```

### Vibecoding workflow (beginner friendly)

1. `make doctor` – fix whatever it reports (it prints the exact commands).
2. `make dev` – TimescaleDB + Mosquitto run in containers, `ingest_worker`, `api_gateway`
   (http://127.0.0.1:8000/docs) and `mcp_server` run locally and **reload automatically** when you
   save a file. Only one service: `make dev SVC=api`. Ctrl-C stops the local services, `make down`
   the containers.
3. Open Claude Code in the repo and use the guided commands – they follow the house rules for you:
   * **`/add-kpi-view <idea>`** – asks what the KPI means, writes an idempotent view
     `db/mart/NN_<kpi>.sql`, activates it with `make apply-sql`, adds a read-only `/v1/...` endpoint
     and the tests.
   * **`/configure-site`** – interactively creates `site_overrides/assets.csv`, `oee_rules.yaml`
     and optional `custom_views.sql` (and checks your PLC payloads against the mapping) without
     touching core code.
4. `make verify`, then commit.

### Database developer workflow

* Views, functions and procedures live in **idempotent** files `db/mart/NN_name.sql`
  (`CREATE OR REPLACE …`, executed in name order, `90_grants.sql` last).
* They are applied automatically on first init **and** any time later with `make apply-sql` –
  no image rebuild, no restart, no data loss. Running it twice must succeed.
* `CREATE OR REPLACE VIEW` cannot rename or drop columns: add new columns at the end or create a
  `_v2` view.
* Tables (`core.*`, `mart` storage tables) are defined in `db/init/` and only change additively.

Layout:

```
db/init/        first-start schema (roles, core tables, mart tables, job registration, grants, pg_hba)
db/mart/        idempotent views / procedures / grants  → `make apply-sql`
services/       shared (TelemetryEvent schema, site config), ingest_worker, api_gateway, mcp_server
mosquitto/      broker config, ACL, entrypoint
grafana/        provisioning + "OEE Live" dashboard
examples/       demo_plant (default seed)
site_overrides/ your plant (git-ignored)
scripts/        env/cert generation, doctor, dev mode, preflight, backup, image export, benchmark
tests/          unit, guardrail (enforces CLAUDE.md rules) + integration
.claude/        guided slash commands (/add-kpi-view, /configure-site)
CLAUDE.md       rules for AI coding agents (AGENTS.md = identical copy)
```

## License

[Apache License 2.0](LICENSE)
