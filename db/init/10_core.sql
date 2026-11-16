-- =============================================================================
-- core: generic, plant-agnostic raw data layer
-- =============================================================================
CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS mart;
CREATE SCHEMA IF NOT EXISTS site;   -- site-specific BI alias views (site_overrides/custom_views.sql)

-- -----------------------------------------------------------------------------
-- Asset hierarchy (site > area > line > machine ...), seeded from assets.csv
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.assets (
    asset_id            text PRIMARY KEY,
    parent_id           text REFERENCES core.assets (asset_id) DEFERRABLE INITIALLY DEFERRED,
    name                text NOT NULL,
    asset_type          text NOT NULL DEFAULT 'machine',
    ideal_cycle_time_s  double precision CHECK (ideal_cycle_time_s IS NULL OR ideal_cycle_time_s > 0),
    attributes          jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CHECK (parent_id IS DISTINCT FROM asset_id)
);
CREATE INDEX IF NOT EXISTS assets_parent_idx ON core.assets (parent_id);

-- -----------------------------------------------------------------------------
-- Analysis rules (synced from oee_rules.yaml). asset_id '*' = plant default.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.oee_rules (
    asset_id         text PRIMARY KEY,
    min_cycle_s      double precision NOT NULL DEFAULT 0.1  CHECK (min_cycle_s > 0),
    max_cycle_s      double precision NOT NULL DEFAULT 3600 CHECK (max_cycle_s > 0),
    microstop_max_s  double precision NOT NULL DEFAULT 300  CHECK (microstop_max_s >= 0),
    histogram_bin_s  double precision NOT NULL DEFAULT 1    CHECK (histogram_bin_s > 0),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CHECK (max_cycle_s > min_cycle_s)
);
INSERT INTO core.oee_rules (asset_id) VALUES ('*') ON CONFLICT DO NOTHING;

-- -----------------------------------------------------------------------------
-- Events: START/END pairs (joined via event_uid) and INSTANT events.
-- 1-day chunks, compression after 7 days (buffer for late offline events).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.events (
    event_datetime  timestamptz      NOT NULL,
    event_uid       text             NOT NULL CHECK (length(event_uid) BETWEEN 1 AND 128),
    event_state     text             NOT NULL CHECK (event_state IN ('START', 'END', 'INSTANT')),
    asset_id        text             NOT NULL CHECK (length(asset_id) BETWEEN 1 AND 128),
    event_category  text             NOT NULL CHECK (event_category IN ('PROCESS', 'ERROR', 'SETUP', 'STANDBY', 'INFO', 'WARNING')),
    event_code      text,
    cycle_time_s    double precision CHECK (cycle_time_s IS NULL OR cycle_time_s > 0),
    attributes      jsonb            NOT NULL DEFAULT '{}'::jsonb,
    source_topic    text,
    received_at     timestamptz      NOT NULL DEFAULT now()
);

SELECT create_hypertable('core.events', by_range('event_datetime', INTERVAL '1 day'), if_not_exists => TRUE);

-- Idempotency key (must contain the partitioning column).
CREATE UNIQUE INDEX IF NOT EXISTS events_idempotency_uq
    ON core.events (event_datetime, event_uid, event_state);
CREATE INDEX IF NOT EXISTS events_pairing_idx
    ON core.events (asset_id, event_uid, event_datetime);
CREATE INDEX IF NOT EXISTS events_received_idx
    ON core.events (received_at DESC);
CREATE INDEX IF NOT EXISTS events_asset_time_idx
    ON core.events (asset_id, event_datetime DESC);

ALTER TABLE core.events SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'asset_id',
    timescaledb.compress_orderby   = 'event_datetime DESC, event_uid, event_state'
);
SELECT add_compression_policy('core.events', INTERVAL '7 days', if_not_exists => TRUE);

-- -----------------------------------------------------------------------------
-- Error log: rejected payloads (ingest) and failing background jobs.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS core.error_logs (
    logged_at   timestamptz NOT NULL DEFAULT now(),
    source      text        NOT NULL,           -- e.g. 'ingest_worker', 'job:refresh_cycle_metrics'
    error_type  text        NOT NULL,           -- e.g. 'json_decode', 'validation', 'db_reject', SQLSTATE
    message     text        NOT NULL,
    topic       text,
    payload     text,                           -- raw payload (truncated by the producer)
    details     jsonb       NOT NULL DEFAULT '{}'::jsonb
);
SELECT create_hypertable('core.error_logs', by_range('logged_at', INTERVAL '7 days'), if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS error_logs_source_idx ON core.error_logs (source, logged_at DESC);
SELECT add_retention_policy('core.error_logs', INTERVAL '90 days', if_not_exists => TRUE);
