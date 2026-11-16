-- Idempotent: safe to re-apply at any time via `make apply-sql`.
-- Only CREATE OR REPLACE / IF NOT EXISTS statements; never drop data here.

-- =============================================================================
-- Standardised BI views (stable contract for Grafana, Power BI, API, MCP)
-- =============================================================================

CREATE OR REPLACE VIEW mart.v_asset_hierarchy AS
WITH RECURSIVE tree AS (
    SELECT a.asset_id, a.parent_id, a.name, a.asset_type, a.ideal_cycle_time_s,
           1 AS level, a.asset_id AS root_id, a.name::text AS path,
           ARRAY[a.asset_id] AS visited
    FROM core.assets a
    WHERE a.parent_id IS NULL
    UNION ALL
    SELECT c.asset_id, c.parent_id, c.name, c.asset_type, c.ideal_cycle_time_s,
           t.level + 1, t.root_id, t.path || ' / ' || c.name,
           t.visited || c.asset_id
    FROM core.assets c
    JOIN tree t ON c.parent_id = t.asset_id
    WHERE NOT c.asset_id = ANY (t.visited)
)
SELECT asset_id, parent_id, name, asset_type, ideal_cycle_time_s, level, root_id, path
FROM tree;

CREATE OR REPLACE VIEW mart.v_cycle_metrics_hourly AS
SELECT m.bucket,
       m.asset_id,
       h.name  AS asset_name,
       h.path  AS asset_path,
       h.ideal_cycle_time_s,
       m.cycle_count, m.cycle_avg_s, m.cycle_median_s, m.cycle_min_s, m.cycle_max_s,
       m.cycle_p25_s, m.cycle_p75_s, m.cycle_p95_s, m.rejected_cycle_count,
       m.microstop_count, m.microstop_s, m.downtime_count, m.downtime_s,
       m.setup_count, m.setup_s, m.updated_at
FROM mart.cycle_metrics m
LEFT JOIN mart.v_asset_hierarchy h USING (asset_id);

CREATE OR REPLACE VIEW mart.v_cycle_metrics_daily AS
SELECT time_bucket(INTERVAL '1 day', i.start_time) AS bucket,
       i.asset_id,
       h.name AS asset_name,
       h.path AS asset_path,
       h.ideal_cycle_time_s,
       count(*) FILTER (WHERE i.stop_class = 'CYCLE' AND i.is_valid)                                         AS cycle_count,
       avg(i.duration_s) FILTER (WHERE i.stop_class = 'CYCLE' AND i.is_valid)                                AS cycle_avg_s,
       percentile_cont(0.50) WITHIN GROUP (ORDER BY i.duration_s) FILTER (WHERE i.stop_class = 'CYCLE' AND i.is_valid) AS cycle_median_s,
       min(i.duration_s) FILTER (WHERE i.stop_class = 'CYCLE' AND i.is_valid)                                AS cycle_min_s,
       max(i.duration_s) FILTER (WHERE i.stop_class = 'CYCLE' AND i.is_valid)                                AS cycle_max_s,
       percentile_cont(0.25) WITHIN GROUP (ORDER BY i.duration_s) FILTER (WHERE i.stop_class = 'CYCLE' AND i.is_valid) AS cycle_p25_s,
       percentile_cont(0.75) WITHIN GROUP (ORDER BY i.duration_s) FILTER (WHERE i.stop_class = 'CYCLE' AND i.is_valid) AS cycle_p75_s,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY i.duration_s) FILTER (WHERE i.stop_class = 'CYCLE' AND i.is_valid) AS cycle_p95_s,
       count(*) FILTER (WHERE i.stop_class = 'CYCLE' AND NOT i.is_valid)                                     AS rejected_cycle_count,
       count(*) FILTER (WHERE i.stop_class = 'MICROSTOP')                                                    AS microstop_count,
       coalesce(sum(i.duration_s) FILTER (WHERE i.stop_class = 'MICROSTOP'), 0)                              AS microstop_s,
       count(*) FILTER (WHERE i.stop_class = 'DOWNTIME')                                                     AS downtime_count,
       coalesce(sum(i.duration_s) FILTER (WHERE i.stop_class = 'DOWNTIME'), 0)                               AS downtime_s,
       count(*) FILTER (WHERE i.stop_class = 'SETUP')                                                        AS setup_count,
       coalesce(sum(i.duration_s) FILTER (WHERE i.stop_class = 'SETUP'), 0)                                  AS setup_s
FROM mart.event_intervals i
LEFT JOIN mart.v_asset_hierarchy h USING (asset_id)
GROUP BY 1, 2, 3, 4, 5;

CREATE OR REPLACE VIEW mart.v_cycle_histogram AS
SELECT c.bucket, c.asset_id, h.name AS asset_name, h.path AS asset_path,
       c.bin_lower_s, c.bin_upper_s, c.cycle_count
FROM mart.cycle_histogram c
LEFT JOIN mart.v_asset_hierarchy h USING (asset_id);

CREATE OR REPLACE VIEW mart.v_stop_events AS
SELECT i.start_time, i.end_time, i.duration_s, i.asset_id,
       h.name AS asset_name, h.path AS asset_path,
       i.event_uid, i.event_category, i.event_code, i.stop_class
FROM mart.event_intervals i
LEFT JOIN mart.v_asset_hierarchy h USING (asset_id)
WHERE i.stop_class IN ('MICROSTOP', 'DOWNTIME', 'SETUP')
  AND i.is_valid;

CREATE OR REPLACE VIEW mart.v_pipeline_health AS
WITH last_event AS (
    SELECT max(received_at) AS last_received_at, max(event_datetime) AS last_event_datetime
    FROM core.events
    WHERE event_datetime > now() - INTERVAL '7 days'
), job AS (
    SELECT ts_value AS last_aggregation_at, details
    FROM mart.pipeline_state WHERE key = 'refresh_cycle_metrics'
), errors AS (
    SELECT count(*) AS errors_last_hour
    FROM core.error_logs WHERE logged_at > now() - INTERVAL '1 hour'
)
SELECT le.last_received_at,
       le.last_event_datetime,
       extract(epoch FROM now() - le.last_received_at)::double precision  AS seconds_since_last_event,
       j.last_aggregation_at,
       extract(epoch FROM now() - j.last_aggregation_at)::double precision AS seconds_since_last_aggregation,
       (j.details ->> 'paired')::integer                                   AS last_paired_count,
       e.errors_last_hour
FROM last_event le
CROSS JOIN errors e
LEFT JOIN job j ON TRUE;

CREATE OR REPLACE VIEW mart.v_error_logs AS
SELECT logged_at, source, error_type, message, topic, left(payload, 500) AS payload_excerpt, details
FROM core.error_logs;
