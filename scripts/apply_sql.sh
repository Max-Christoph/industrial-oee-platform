#!/usr/bin/env bash
# Applies all idempotent SQL files from db/mart/ (views, functions, procedures, grants)
# to the running database - no rebuild, no restart, no data loss.
# Optionally also site_overrides/custom_views.sql (BI alias views) with --site.
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE_CMD="${COMPOSE:-podman compose}"
DB="$(grep -E '^POSTGRES_DB=' .env 2>/dev/null | cut -d= -f2- || true)"
DB="${DB:-oee}"

psql_in_db() {
  $COMPOSE_CMD exec -T timescaledb psql -v ON_ERROR_STOP=1 -U postgres -d "$DB" "$@"
}

shopt -s nullglob
files=(db/mart/*.sql)
if [ ${#files[@]} -eq 0 ]; then
  echo "no SQL files in db/mart/"; exit 0
fi

for f in "${files[@]}"; do
  echo ">> applying $f"
  # Each file runs in a single transaction: either fully applied or not at all.
  psql_in_db --single-transaction -q < "$f"
done

if [ "${1:-}" = "--site" ] && [ -f site_overrides/custom_views.sql ]; then
  echo ">> applying site_overrides/custom_views.sql (as oee_app)"
  { echo "SET ROLE oee_app;"; cat site_overrides/custom_views.sql; } | psql_in_db --single-transaction -q
fi
echo "done."
