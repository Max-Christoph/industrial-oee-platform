---
description: Guided, interactive setup of site_overrides/ (assets.csv, oee_rules.yaml, custom_views.sql) without touching core code
argument-hint: "[optional: what you want to configure, e.g. 'assets' or 'mapping']"
---

# /configure-site – configure your plant (only in `site_overrides/`)

You are helping a colleague configure this platform for **their** plant. They may be new to IT.
Go step by step, ask short questions, show what you will write, and confirm before writing.
Focus: $ARGUMENTS

## Hard rules (from CLAUDE.md)
- Write **only** inside `site_overrides/` (git-ignored). Never edit `services/`, `db/`, `examples/`,
  `docker-compose.yml` or other tracked files in this workflow.
- Real plant data (machine names, server names, SAP numbers …) is fine **inside `site_overrides/`**
  but must never be copied into tracked files, commit messages or examples.
- Never commit anything from this workflow. Remind the user that `site_overrides/` is not in git and
  should be backed up separately (e.g. together with `.env`).
- Core identifiers stay generic English; localised names only as aliases in `custom_views.sql`.

## Step 0 – Starting point
1. Check what already exists: `ls site_overrides/` and read `site_overrides/README.md`.
2. If a file already exists, show a short summary and ask whether to extend or replace it
   (default: extend; make a backup copy `<file>.bak` before replacing).
3. Otherwise start from the demo files in `examples/demo_plant/` as templates (read them, do not modify them).

Ask which parts to configure (multiple allowed): **A** assets, **B** rules & payload mapping,
**C** BI alias views, **D** TLS certificates.

## A – `site_overrides/assets.csv` (asset hierarchy)
Ask for: plant name, areas/halls, lines, machines, and (optional) ideal cycle time per machine in seconds.
Build IDs as short, stable, uppercase codes without spaces (e.g. `PLANT-1`, `LINE-01`, `LINE-01-M01`).
Format (header required, UTF-8):
```csv
asset_id,parent_id,name,asset_type,ideal_cycle_time_s
PLANT-1,,<plant name>,site,
HALL-A,PLANT-1,<hall name>,area,
LINE-01,HALL-A,<line name>,line,
LINE-01-M01,LINE-01,<machine name>,machine,12.0
```
Validate before writing: unique `asset_id`, every `parent_id` exists, no cycles, `ideal_cycle_time_s`
empty or > 0. You can check with:
```bash
SITE_OVERRIDES_DIR=site_overrides uv run python -c "from oee_shared.site_config import load_assets; print(len(load_assets()), 'assets OK')"
```

## B – `site_overrides/oee_rules.yaml` (thresholds + payload mapping)
Ask (with sensible defaults in brackets):
- shortest plausible cycle `min_cycle_s` [1], longest `max_cycle_s` [600]
- stop shorter than `microstop_max_s` counts as microstop, longer as downtime [300]
- histogram bin width `histogram_bin_s` [1]
- per-machine exceptions?
- How do the PLCs/gateways send data? Ask for **one example payload** and the MQTT topic. Then derive:
  - `topic_asset_regex` (must contain `(?P<asset_id>...)`) if the asset is only in the topic,
  - `field_aliases` (their field name → `event_uid`, `event_datetime`, `event_state`,
    `event_category`, `asset_id`, `event_code`, `cycle_time_s`),
  - `category_aliases` (their status words → `PROCESS`, `ERROR`, `SETUP`, `STANDBY`, `INFO`, `WARNING`),
  - `state_aliases` (→ `START`, `END`, `INSTANT`),
  - `asset_aliases` (PLC names → `asset_id` from assets.csv).
- Do PLCs send local time without zone? Then `SITE_TIMEZONE` in `.env` must be the plant time zone
  (e.g. `Europe/Berlin`) – check it and tell the user.

Use `examples/demo_plant/oee_rules.yaml` as structure. Validate:
```bash
SITE_OVERRIDES_DIR=site_overrides uv run python -c "from oee_shared.site_config import load_rules; r=load_rules(); print(r.effective_rows())"
```
Then prove the mapping with their example payload:
```bash
SITE_OVERRIDES_DIR=site_overrides uv run python -c "
from oee_shared.site_config import load_rules; from oee_shared import ValidationLimits
from ingest_worker.parser import parse_payload
r = parse_payload('<topic>', '''<payload>''', load_rules().mapping, ValidationLimits(site_timezone='<tz>'))
print(r.events, r.errors)"
```
If `errors` is not empty, adjust the aliases and test again.

## C – `site_overrides/custom_views.sql` (optional BI aliases)
For Power BI users who want localised column names or plant-specific shortcuts. Rules:
- only `CREATE OR REPLACE VIEW site.<name> AS SELECT ... FROM mart.v_... ;`
- build on `mart.v_*` views only (never on `core.*` tables), no calculations beyond renaming/
  rounding/filtering (real KPI logic belongs in `db/mart/` → use `/add-kpi-view`).
Example:
```sql
CREATE OR REPLACE VIEW site.taktzeiten_taeglich AS
SELECT bucket AS tag_utc, asset_path AS anlage, cycle_count AS takte, cycle_median_s AS taktzeit_median_s
FROM mart.v_cycle_metrics_daily;
```

## D – TLS certificates (optional)
Corporate PKI: place `server.crt` (incl. intermediate chain), `server.key` and `ca.crt` in
`site_overrides/certs/`. The certificate must contain the DNS names clients use (check with
`openssl x509 -in site_overrides/certs/server.crt -noout -ext subjectAltName`). Without own
certificates `make certs` generates a self-signed CA (10 years). Never print private keys.

## Step Z – Activate and verify
```bash
make apply-site          # restarts ingest_worker: syncs rules + assets, applies custom views
make logs S=ingest_worker   # look for "synced ... rule rows and ... assets" and "applied custom_views.sql"
```
If the stack is not running yet: `make up`. Finally show a summary of the created files and remind:
back up `site_overrides/` and `.env`; nothing from here goes into git (`git status` must not list them).
