# industrial-oee-platform - all standard workflows
# Usage: make help      New here? Run: make doctor
#
# House rules (details in CLAUDE.md / AGENTS.md):
#   * SQL = domain logic, Python = transport. KPIs, percentiles, stop classification live ONLY in
#     db/mart/*.sql (idempotent CREATE OR REPLACE ...) and are activated with `make apply-sql`.
#   * Never change core.* destructively; read-only roles get SELECT only.
#   * Before every commit: `make verify` (lint + tests + compose config).

SHELL       := /usr/bin/env bash
COMPOSE     ?= podman compose
ENGINE      ?= podman
export COMPOSE ENGINE

# Read a few values (ports, DB name) from .env for messages and exec targets.
-include .env
POSTGRES_DB ?= oee
export POSTGRES_DB GRAFANA_PORT API_PORT MCP_PORT

.DEFAULT_GOAL := help
.PHONY: help doctor dev preflight env certs setup up down restart ps logs test test-unit test-integration lint verify \
        bench backup restore export-images import-images apply-sql apply-site psql config reset

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --------------------------------------------------------------------------- onboarding
doctor: ## Check podman, compose, uv, linger, free ports, .env, certs - prints copy-paste fixes
	@scripts/doctor.sh

dev: setup ## Dev mode: DB + MQTT in containers, Python services locally with hot reload (SVC=api|ingest|mcp)
	@scripts/dev.sh

# --------------------------------------------------------------------------- setup
preflight: ## Check podman, compose and `loginctl enable-linger` (24/7 rootless)
	@scripts/preflight.sh

env: ## Create .env with random secrets (never overwrites)
	@scripts/init_env.sh

certs: ## Use PKI certs from site_overrides/certs or generate self-signed (10y)
	@scripts/gen_certs.sh

setup: env certs ## env + certs

# --------------------------------------------------------------------------- lifecycle
up: preflight setup ## Build and start the whole stack
	$(COMPOSE) up -d --build
	@echo "Grafana: http://localhost:$${GRAFANA_PORT:-3000}  API: http://localhost:$${API_PORT:-8000}/docs  MCP: http://127.0.0.1:$${MCP_PORT:-8001}/mcp"

down: ## Stop the stack (data volumes are kept)
	$(COMPOSE) down

restart: ## Restart all services
	$(COMPOSE) restart

ps: ## Container status incl. health
	$(COMPOSE) ps

logs: ## Follow logs (S=<service> for a single one)
	$(COMPOSE) logs -f --tail=200 $(S)

config: ## Validate and render the compose file
	$(COMPOSE) config

# --------------------------------------------------------------------------- database
apply-sql: ## Re-apply idempotent db/mart/*.sql (views, functions, grants) to the running DB - no rebuild, no data loss
	@scripts/apply_sql.sh

apply-site: ## Re-sync site_overrides (rules, assets, custom views) by restarting ingest_worker
	$(COMPOSE) restart ingest_worker

psql: ## Open psql as superuser
	$(COMPOSE) exec timescaledb psql -U postgres -d $${POSTGRES_DB:-oee}

backup: ## pg_dump (custom format) into ./backups (rotates, keeps BACKUP_KEEP=14)
	@scripts/backup.sh

restore: ## Restore a dump: make restore FILE=backups/oee_....dump
	@test -n "$(FILE)" || (echo "usage: make restore FILE=backups/<file>.dump" && exit 1)
	$(COMPOSE) stop ingest_worker api_gateway mcp_server grafana
	$(COMPOSE) exec -T timescaledb psql -U postgres -d $${POSTGRES_DB:-oee} -c "SELECT timescaledb_pre_restore();"
	$(COMPOSE) exec -T timescaledb pg_restore -U postgres -d $${POSTGRES_DB:-oee} --clean --if-exists --no-owner < $(FILE) || true
	$(COMPOSE) exec -T timescaledb psql -U postgres -d $${POSTGRES_DB:-oee} -c "SELECT timescaledb_post_restore();"
	$(COMPOSE) start ingest_worker api_gateway mcp_server grafana

# --------------------------------------------------------------------------- quality
test: ## Run the pytest suite (integration tests run if OEE_TEST_DSN is set)
	uv sync --all-packages --frozen
	uv run pytest

test-unit: ## Unit tests only
	uv run pytest tests/unit

test-integration: ## Integration tests against a DB: OEE_TEST_DSN=postgresql://postgres:...@localhost:5432/postgres
	uv run pytest tests/integration -m integration

lint: ## Ruff lint
	uv run ruff check .

verify: lint test config ## Definition of Done: lint + tests + compose config (run before every commit)
	@echo "verify: OK"

bench: ## Ingest benchmark 100/250/500 ev/s (BENCH_RATES, BENCH_DURATION) - stack must be up
	$(COMPOSE) exec -T -e BENCH_RATES=$${BENCH_RATES:-100,250,500} -e BENCH_DURATION=$${BENCH_DURATION:-30} \
		ingest_worker python /app/scripts/benchmark.py

# --------------------------------------------------------------------------- air-gap
export-images: ## Save all images to dist/oee-images.tar for offline hosts
	@scripts/export_images.sh

import-images: ## Load dist/oee-images.tar on an offline host
	$(ENGINE) load -i dist/oee-images.tar

# --------------------------------------------------------------------------- danger
reset: ## DELETE all data volumes and start fresh (asks for confirmation)
	@read -r -p "This deletes ALL data (DB, MQTT queue, Grafana). Type 'yes' to continue: " ans; \
	  [ "$$ans" = "yes" ] || { echo "aborted"; exit 1; }
	$(COMPOSE) down -v
	@echo "volumes removed. Run 'make up' to start fresh."
