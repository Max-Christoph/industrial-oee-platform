# site_overrides/

Everything plant- or IT-specific lives here. The directory is **git-ignored**
(except this README and `certs/.gitkeep`) – nothing in it is ever committed.

Each file overrides its counterpart in `examples/demo_plant/` (per-file fallback):

| File | Purpose | Applied |
|------|---------|---------|
| `oee_rules.yaml` | analysis thresholds + payload mapping (field/category/state/asset aliases) | ingest_worker start (`make apply-site`) |
| `assets.csv` | asset hierarchy (`asset_id,parent_id,name,asset_type,ideal_cycle_time_s`) | ingest_worker start (upsert) |
| `custom_views.sql` | BI alias views in schema `site` (e.g. localized column names) | ingest_worker start / `scripts/apply_sql.sh --site` |
| `certs/server.crt`, `certs/server.key`, `certs/ca.crt` | corporate PKI certificates for PostgreSQL + Mosquitto | container start |

If `certs/` is empty, `make up` generates a self-signed CA + server certificate (10 years).
Start from the demo files:

```bash
cp examples/demo_plant/oee_rules.yaml examples/demo_plant/assets.csv site_overrides/
```
