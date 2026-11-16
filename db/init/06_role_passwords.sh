#!/bin/bash
# Enables LOGIN for the service roles and sets their passwords from the
# container environment (generated into .env by `make env`).
set -euo pipefail

for var in OEE_APP_PASSWORD GRAFANA_DB_PASSWORD BI_DB_PASSWORD AI_DB_PASSWORD; do
  if [ -z "${!var:-}" ]; then
    echo "FATAL: $var is empty" >&2
    exit 1
  fi
done

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v app_pw="$OEE_APP_PASSWORD" \
  -v grafana_pw="$GRAFANA_DB_PASSWORD" \
  -v bi_pw="$BI_DB_PASSWORD" \
  -v ai_pw="$AI_DB_PASSWORD" <<'SQL'
ALTER ROLE oee_app        LOGIN PASSWORD :'app_pw';
ALTER ROLE grafana_reader LOGIN PASSWORD :'grafana_pw';
ALTER ROLE bi_reader      LOGIN PASSWORD :'bi_pw';
ALTER ROLE ai_reader      LOGIN PASSWORD :'ai_pw';
SQL
