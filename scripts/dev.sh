#!/usr/bin/env bash
# `make dev` - DB + MQTT in containers, Python services locally with hot reload.
#   make dev            -> starts infra and all three services (Ctrl-C stops the local services)
#   make dev SVC=api    -> only one service (api | ingest | mcp)
# Infrastructure keeps running after Ctrl-C; stop it with `make down`.
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE_CMD="${COMPOSE:-podman compose}"
SVC="${SVC:-all}"
[ -f .env ] || { echo ".env missing - run: make env" >&2; exit 1; }
envval() { grep -E "^$1=" .env | tail -1 | cut -d= -f2-; }

DB="$(envval POSTGRES_DB)"; DB="${DB:-oee}"
PG_PORT="$(envval PG_PORT)"; PG_PORT="${PG_PORT:-5432}"
MQTT_DEV_PORT="$(envval MQTT_DEV_PORT)"; MQTT_DEV_PORT="${MQTT_DEV_PORT:-1883}"
PG="localhost:${PG_PORT}/${DB}?sslmode=require"

echo ">> starting infrastructure (timescaledb, mosquitto)"
$COMPOSE_CMD -f docker-compose.yml -f docker-compose.dev.yml up -d --wait timescaledb mosquitto \
  || $COMPOSE_CMD -f docker-compose.yml -f docker-compose.dev.yml up -d timescaledb mosquitto

uv sync --all-packages --quiet

export SITE_TIMEZONE="$(envval SITE_TIMEZONE)" LOG_LEVEL="${LOG_LEVEL:-DEBUG}"
export SITE_OVERRIDES_DIR="$PWD/site_overrides" SITE_DEFAULTS_DIR="$PWD/examples/demo_plant"
WATCH=(services/shared/src)
pids=()
cleanup() { echo; echo ">> stopping local services"; kill "${pids[@]}" 2>/dev/null || true; wait 2>/dev/null; }
trap cleanup INT TERM EXIT

if [[ "$SVC" == all || "$SVC" == ingest ]]; then
  echo ">> ingest_worker (reload on changes in services/shared, services/ingest_worker)"
  DATABASE_URL="postgresql://oee_app:$(envval OEE_APP_PASSWORD)@${PG}" \
  MQTT_HOST=localhost MQTT_PORT="$MQTT_DEV_PORT" \
  MQTT_USERNAME="$(envval MQTT_INGEST_USER)" MQTT_PASSWORD="$(envval MQTT_INGEST_PASSWORD)" \
  MQTT_CLIENT_ID="oee-ingest-dev" HEARTBEAT_FILE="${TMPDIR:-/tmp}/oee_ingest_dev_heartbeat" \
    uv run watchfiles --filter python "python -m ingest_worker" "${WATCH[@]}" services/ingest_worker/src &
  pids+=($!)
fi
if [[ "$SVC" == all || "$SVC" == api ]]; then
  echo ">> api_gateway on http://127.0.0.1:8000/docs (uvicorn --reload)"
  DATABASE_URL="postgresql://bi_reader:$(envval BI_DB_PASSWORD)@${PG}" API_KEYS="$(envval API_KEYS)" \
    uv run uvicorn api_gateway.app:app --host 127.0.0.1 --port 8000 --reload \
      --reload-dir services/api_gateway/src --reload-dir services/shared/src &
  pids+=($!)
fi
if [[ "$SVC" == all || "$SVC" == mcp ]]; then
  echo ">> mcp_server on http://127.0.0.1:8001/mcp (reload on changes)"
  DATABASE_URL="postgresql://ai_reader:$(envval AI_DB_PASSWORD)@${PG}" \
    uv run watchfiles --filter python "python -m mcp_server --host 127.0.0.1 --port 8001" \
      "${WATCH[@]}" services/mcp_server/src &
  pids+=($!)
fi

echo ">> running - edit code, services reload automatically. Ctrl-C to stop."
wait
