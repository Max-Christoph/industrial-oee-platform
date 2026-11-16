-- =============================================================================
-- mart: derived analytics (filled by the TimescaleDB background job in 30_jobs.sql)
-- =============================================================================

-- Paired START/END intervals (one row per asset_id + event_uid).
CREATE TABLE IF NOT EXISTS mart.event_intervals (
    asset_id               text             NOT NULL,
    event_uid              text             NOT NULL,
    event_category         text             NOT NULL,
    event_code             text,
    start_time             timestamptz      NOT NULL,
    end_time               timestamptz      NOT NULL,
    duration_s             double precision NOT NULL,
    reported_cycle_time_s  double precision,
    stop_class             text CHECK (stop_class IN ('CYCLE', 'MICROSTOP', 'DOWNTIME', 'SETUP')),
    is_valid               boolean          NOT NULL,
    invalid_reason         text,
    paired_at              timestamptz      NOT NULL DEFAULT now(),
    PRIMARY KEY (asset_id, event_uid, start_time)
);
SELECT create_hypertable('mart.event_intervals', by_range('start_time', INTERVAL '7 days'), if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS event_intervals_asset_time_idx ON mart.event_intervals (asset_id, start_time DESC);
CREATE INDEX IF NOT EXISTS event_intervals_stop_idx
    ON mart.event_intervals (stop_class, start_time DESC) WHERE stop_class IS NOT NULL;

ALTER TABLE mart.event_intervals SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'asset_id',
    timescaledb.compress_orderby   = 'start_time DESC, event_uid'
);
SELECT add_compression_policy('mart.event_intervals', INTERVAL '30 days', if_not_exists => TRUE);

-- Hourly cycle-time & stop statistics per asset (bucket = UTC hour).
CREATE TABLE IF NOT EXISTS mart.cycle_metrics (
    bucket                timestamptz      NOT NULL,
    asset_id              text             NOT NULL,
    cycle_count           integer          NOT NULL DEFAULT 0,
    cycle_avg_s           double precision,
    cycle_median_s        double precision,
    cycle_min_s           double precision,
    cycle_max_s           double precision,
    cycle_p25_s           double precision,
    cycle_p75_s           double precision,
    cycle_p95_s           double precision,
    rejected_cycle_count  integer          NOT NULL DEFAULT 0,
    microstop_count       integer          NOT NULL DEFAULT 0,
    microstop_s           double precision NOT NULL DEFAULT 0,
    downtime_count        integer          NOT NULL DEFAULT 0,
    downtime_s            double precision NOT NULL DEFAULT 0,
    setup_count           integer          NOT NULL DEFAULT 0,
    setup_s               double precision NOT NULL DEFAULT 0,
    updated_at            timestamptz      NOT NULL DEFAULT now(),
    PRIMARY KEY (asset_id, bucket)
);
SELECT create_hypertable('mart.cycle_metrics', by_range('bucket', INTERVAL '30 days'), if_not_exists => TRUE);

-- Daily cycle-time distribution per asset (bin width from core.oee_rules).
CREATE TABLE IF NOT EXISTS mart.cycle_histogram (
    bucket       timestamptz      NOT NULL,   -- UTC day
    asset_id     text             NOT NULL,
    bin_lower_s  double precision NOT NULL,
    bin_upper_s  double precision NOT NULL,
    cycle_count  integer          NOT NULL,
    updated_at   timestamptz      NOT NULL DEFAULT now(),
    PRIMARY KEY (asset_id, bucket, bin_lower_s)
);
SELECT create_hypertable('mart.cycle_histogram', by_range('bucket', INTERVAL '90 days'), if_not_exists => TRUE);

-- Job bookkeeping (watermark, last run) for pipeline-health monitoring.
CREATE TABLE IF NOT EXISTS mart.pipeline_state (
    key         text PRIMARY KEY,
    ts_value    timestamptz,
    details     jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Views and functions live in db/mart/*.sql (idempotent; applied by 25_apply_mart.sh and `make apply-sql`).
