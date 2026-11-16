-- Idempotent, purely ADDITIVE schema additions (safe via `make apply-sql`).
-- Applied on fresh installs right after db/init/20_mart.sql and on existing
-- installations to bring them to the current schema. Never drop/rename/retype here.

-- Produced quantities per paired interval (optional; from quantity_good / quantity_scrap
-- sent with the events). NULL = not reported.
ALTER TABLE mart.event_intervals ADD COLUMN IF NOT EXISTS quantity_good  double precision;
ALTER TABLE mart.event_intervals ADD COLUMN IF NOT EXISTS quantity_scrap double precision;

-- Hourly produced quantities (a valid cycle without reported quantities = 1 good unit).
ALTER TABLE mart.cycle_metrics ADD COLUMN IF NOT EXISTS good_quantity  double precision NOT NULL DEFAULT 0;
ALTER TABLE mart.cycle_metrics ADD COLUMN IF NOT EXISTS scrap_quantity double precision NOT NULL DEFAULT 0;

-- Planned production time (shift model), synced from `planned_time` in oee_rules.yaml.
-- Shifts are wall-clock definitions: local `time` + explicit IANA time zone, converted
-- to UTC in SQL (mart.planned_seconds). No rows = 24/7 planned production.
-- asset_id '*' = default for all assets without own patterns.
CREATE TABLE IF NOT EXISTS core.shift_patterns (
    asset_id     text     NOT NULL,
    iso_weekday  smallint NOT NULL CHECK (iso_weekday BETWEEN 1 AND 7),   -- 1 = Monday (day the shift starts)
    start_local  time     NOT NULL,
    end_local    time     NOT NULL,                                       -- end <= start = ends next day
    timezone     text     NOT NULL,
    shift_name   text,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (asset_id, iso_weekday, start_local),
    CHECK (start_local <> end_local)
);
