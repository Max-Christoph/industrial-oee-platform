#!/bin/bash
# First-init: apply all idempotent mart SQL files (same files as `make apply-sql`).
set -euo pipefail
for f in /opt/oee-mart/*.sql; do
  echo "applying $f"
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -f "$f"
done
