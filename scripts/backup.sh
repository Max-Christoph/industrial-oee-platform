#!/usr/bin/env bash
# Consistent logical backup (pg_dump custom format) into ./backups/.
# Restore: see README "Backup & Restore".
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE_CMD="${COMPOSE:-podman compose}"
DB="$(grep -E '^POSTGRES_DB=' .env 2>/dev/null | cut -d= -f2- || true)"
DB="${DB:-oee}"
KEEP="${BACKUP_KEEP:-14}"
mkdir -p backups
out="backups/${DB}_$(date -u +%Y%m%dT%H%M%SZ).dump"

$COMPOSE_CMD exec -T timescaledb pg_dump -U postgres -d "$DB" -Fc -Z 6 > "$out.partial"
mv "$out.partial" "$out"
echo "backup written: $out ($(du -h "$out" | cut -f1))"

# rotate
ls -1t backups/*.dump 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f
