-- Idempotent: safe to re-apply at any time via `make apply-sql`.
-- =============================================================================
-- Native TimescaleDB background job: incremental START/END pairing via event_uid
-- and recomputation of the affected hourly metrics / daily histograms.
-- No external scheduler, no Python container required.
--
-- config:
--   lookback  – how far back late START/END partners are searched (default 7 days,
--               aligned with the compression horizon of core.events)
--   overlap   – safety overlap on the received_at watermark for transactions that
--               committed after the previous run started (default 5 minutes)
-- =============================================================================

CREATE OR REPLACE PROCEDURE mart.refresh_cycle_metrics(job_id integer DEFAULT 0, config jsonb DEFAULT '{}'::jsonb)
LANGUAGE plpgsql
AS $proc$
DECLARE
    v_lookback   interval;
    v_overlap    interval;
    v_started    timestamptz := clock_timestamp();
    v_snapshot   timestamptz := now();
    v_watermark  timestamptz;
    v_paired     bigint      := 0;
    v_buckets    bigint      := 0;
    v_msg        text;
    v_state      text;
    v_ctx        text;
BEGIN
    BEGIN
        -- parsed inside the protected block so an invalid job config is logged, not raised
        v_lookback := coalesce(config ->> 'lookback', '7 days')::interval;
        v_overlap  := coalesce(config ->> 'overlap', '5 minutes')::interval;

        SELECT ts_value INTO v_watermark FROM mart.pipeline_state WHERE key = 'pairing_watermark';
        v_watermark := coalesce(v_watermark, '-infinity'::timestamptz);

        CREATE TEMP TABLE IF NOT EXISTS _oee_touched (
            asset_id   text        NOT NULL,
            start_time timestamptz NOT NULL
        ) ON COMMIT DROP;
        DELETE FROM _oee_touched;  -- temp table, per-run scratch space

        -- 1) Pair START/END events of every event_uid that received new data.
        WITH touched AS (
            SELECT DISTINCT e.asset_id, e.event_uid
            FROM core.events e
            WHERE e.received_at > v_watermark - v_overlap
              AND e.event_datetime > v_snapshot - v_lookback
              AND e.event_state IN ('START', 'END')
        ), agg AS (
            SELECT e.asset_id,
                   e.event_uid,
                   min(e.event_datetime) FILTER (WHERE e.event_state = 'START') AS start_time,
                   max(e.event_datetime) FILTER (WHERE e.event_state = 'END')   AS end_time,
                   (array_agg(e.event_category ORDER BY (e.event_state = 'END'), e.event_datetime))[1] AS event_category,
                   (array_agg(e.event_code ORDER BY (e.event_state = 'END'), e.event_datetime)
                        FILTER (WHERE e.event_code IS NOT NULL))[1]             AS event_code,
                   max(e.cycle_time_s)                                          AS reported_cycle_time_s,
                   -- optional produced quantities (validated numbers from the ingest schema;
                   -- jsonb_typeof guard keeps the job alive on hand-written attributes)
                   max(CASE WHEN jsonb_typeof(e.attributes -> 'quantity_good') = 'number'
                            THEN (e.attributes ->> 'quantity_good')::double precision END)  AS quantity_good,
                   max(CASE WHEN jsonb_typeof(e.attributes -> 'quantity_scrap') = 'number'
                            THEN (e.attributes ->> 'quantity_scrap')::double precision END) AS quantity_scrap
            FROM core.events e
            JOIN touched t ON t.asset_id = e.asset_id AND t.event_uid = e.event_uid
            WHERE e.event_datetime > v_snapshot - 2 * v_lookback
              AND e.event_state IN ('START', 'END')
            GROUP BY e.asset_id, e.event_uid
        ), ruled AS (
            SELECT a.*,
                   extract(epoch FROM a.end_time - a.start_time)::double precision AS duration_s,
                   coalesce(r.min_cycle_s,     d.min_cycle_s,     0.1)  AS min_cycle_s,
                   coalesce(r.max_cycle_s,     d.max_cycle_s,     3600) AS max_cycle_s,
                   coalesce(r.microstop_max_s, d.microstop_max_s, 300)  AS microstop_max_s
            FROM agg a
            LEFT JOIN core.oee_rules r ON r.asset_id = a.asset_id
            LEFT JOIN core.oee_rules d ON d.asset_id = '*'
            WHERE a.start_time IS NOT NULL
              AND a.end_time IS NOT NULL
        ), classified AS (
            SELECT x.*,
                   CASE
                       WHEN x.event_category = 'PROCESS' THEN 'CYCLE'
                       WHEN x.event_category = 'SETUP'   THEN 'SETUP'
                       WHEN x.event_category IN ('ERROR', 'STANDBY') AND x.duration_s < x.microstop_max_s THEN 'MICROSTOP'
                       WHEN x.event_category IN ('ERROR', 'STANDBY') THEN 'DOWNTIME'
                   END AS stop_class,
                   CASE
                       WHEN x.duration_s <= 0 THEN 'non_positive_duration'
                       WHEN x.event_category = 'PROCESS' AND x.duration_s < x.min_cycle_s THEN 'below_min_cycle'
                       WHEN x.event_category = 'PROCESS' AND x.duration_s > x.max_cycle_s THEN 'above_max_cycle'
                   END AS invalid_reason
            FROM ruled x
        ), upserted AS (
            INSERT INTO mart.event_intervals AS t (
                asset_id, event_uid, event_category, event_code, start_time, end_time,
                duration_s, reported_cycle_time_s, stop_class, is_valid, invalid_reason, paired_at,
                quantity_good, quantity_scrap)
            SELECT c.asset_id, c.event_uid, c.event_category, c.event_code, c.start_time, c.end_time,
                   c.duration_s, c.reported_cycle_time_s, c.stop_class, c.invalid_reason IS NULL,
                   c.invalid_reason, v_snapshot, c.quantity_good, c.quantity_scrap
            FROM classified c
            ON CONFLICT (asset_id, event_uid, start_time) DO UPDATE
               SET end_time              = EXCLUDED.end_time,
                   event_category        = EXCLUDED.event_category,
                   event_code            = EXCLUDED.event_code,
                   duration_s            = EXCLUDED.duration_s,
                   reported_cycle_time_s = EXCLUDED.reported_cycle_time_s,
                   stop_class            = EXCLUDED.stop_class,
                   is_valid              = EXCLUDED.is_valid,
                   invalid_reason        = EXCLUDED.invalid_reason,
                   paired_at             = EXCLUDED.paired_at,
                   quantity_good         = EXCLUDED.quantity_good,
                   quantity_scrap        = EXCLUDED.quantity_scrap
             WHERE (t.end_time, t.event_category, t.event_code, t.reported_cycle_time_s, t.stop_class, t.is_valid,
                    t.quantity_good, t.quantity_scrap)
                   IS DISTINCT FROM
                   (EXCLUDED.end_time, EXCLUDED.event_category, EXCLUDED.event_code,
                    EXCLUDED.reported_cycle_time_s, EXCLUDED.stop_class, EXCLUDED.is_valid,
                    EXCLUDED.quantity_good, EXCLUDED.quantity_scrap)
            RETURNING t.asset_id, t.start_time
        )
        INSERT INTO _oee_touched (asset_id, start_time)
        SELECT asset_id, start_time FROM upserted;
        GET DIAGNOSTICS v_paired = ROW_COUNT;

        -- 2) Recompute hourly metrics for every touched (asset, hour).
        INSERT INTO mart.cycle_metrics AS m (
            bucket, asset_id, cycle_count, cycle_avg_s, cycle_median_s, cycle_min_s, cycle_max_s,
            cycle_p25_s, cycle_p75_s, cycle_p95_s, rejected_cycle_count,
            microstop_count, microstop_s, downtime_count, downtime_s, setup_count, setup_s,
            good_quantity, scrap_quantity, updated_at)
        SELECT b.bucket,
               b.asset_id,
               count(*) FILTER (WHERE i.stop_class = 'CYCLE' AND i.is_valid),
               avg(i.duration_s) FILTER (WHERE i.stop_class = 'CYCLE' AND i.is_valid),
               percentile_cont(0.50) WITHIN GROUP (ORDER BY i.duration_s) FILTER (WHERE i.stop_class = 'CYCLE' AND i.is_valid),
               min(i.duration_s) FILTER (WHERE i.stop_class = 'CYCLE' AND i.is_valid),
               max(i.duration_s) FILTER (WHERE i.stop_class = 'CYCLE' AND i.is_valid),
               percentile_cont(0.25) WITHIN GROUP (ORDER BY i.duration_s) FILTER (WHERE i.stop_class = 'CYCLE' AND i.is_valid),
               percentile_cont(0.75) WITHIN GROUP (ORDER BY i.duration_s) FILTER (WHERE i.stop_class = 'CYCLE' AND i.is_valid),
               percentile_cont(0.95) WITHIN GROUP (ORDER BY i.duration_s) FILTER (WHERE i.stop_class = 'CYCLE' AND i.is_valid),
               count(*) FILTER (WHERE i.stop_class = 'CYCLE' AND NOT i.is_valid),
               count(*) FILTER (WHERE i.stop_class = 'MICROSTOP' AND i.is_valid),
               coalesce(sum(i.duration_s) FILTER (WHERE i.stop_class = 'MICROSTOP' AND i.is_valid), 0),
               count(*) FILTER (WHERE i.stop_class = 'DOWNTIME' AND i.is_valid),
               coalesce(sum(i.duration_s) FILTER (WHERE i.stop_class = 'DOWNTIME' AND i.is_valid), 0),
               count(*) FILTER (WHERE i.stop_class = 'SETUP' AND i.is_valid),
               coalesce(sum(i.duration_s) FILTER (WHERE i.stop_class = 'SETUP' AND i.is_valid), 0),
               -- a valid cycle without reported quantities counts as one good unit
               coalesce(sum(coalesce(i.quantity_good, CASE WHEN i.quantity_scrap IS NULL THEN 1 ELSE 0 END))
                        FILTER (WHERE i.stop_class = 'CYCLE' AND i.is_valid), 0),
               coalesce(sum(coalesce(i.quantity_scrap, 0)) FILTER (WHERE i.stop_class = 'CYCLE' AND i.is_valid), 0),
               clock_timestamp()
        FROM (SELECT DISTINCT asset_id, time_bucket(INTERVAL '1 hour', start_time) AS bucket FROM _oee_touched) b
        JOIN mart.event_intervals i
          ON i.asset_id = b.asset_id
         AND i.start_time >= b.bucket
         AND i.start_time <  b.bucket + INTERVAL '1 hour'
        GROUP BY b.bucket, b.asset_id
        ON CONFLICT (asset_id, bucket) DO UPDATE
           SET cycle_count          = EXCLUDED.cycle_count,
               cycle_avg_s          = EXCLUDED.cycle_avg_s,
               cycle_median_s       = EXCLUDED.cycle_median_s,
               cycle_min_s          = EXCLUDED.cycle_min_s,
               cycle_max_s          = EXCLUDED.cycle_max_s,
               cycle_p25_s          = EXCLUDED.cycle_p25_s,
               cycle_p75_s          = EXCLUDED.cycle_p75_s,
               cycle_p95_s          = EXCLUDED.cycle_p95_s,
               rejected_cycle_count = EXCLUDED.rejected_cycle_count,
               microstop_count      = EXCLUDED.microstop_count,
               microstop_s          = EXCLUDED.microstop_s,
               downtime_count       = EXCLUDED.downtime_count,
               downtime_s           = EXCLUDED.downtime_s,
               setup_count          = EXCLUDED.setup_count,
               setup_s              = EXCLUDED.setup_s,
               good_quantity        = EXCLUDED.good_quantity,
               scrap_quantity       = EXCLUDED.scrap_quantity,
               updated_at           = EXCLUDED.updated_at;
        GET DIAGNOSTICS v_buckets = ROW_COUNT;

        -- 3) Rebuild daily histograms for every touched (asset, day).
        DELETE FROM mart.cycle_histogram h
        USING (SELECT DISTINCT asset_id, time_bucket(INTERVAL '1 day', start_time) AS bucket FROM _oee_touched) d
        WHERE h.asset_id = d.asset_id AND h.bucket = d.bucket;

        INSERT INTO mart.cycle_histogram (bucket, asset_id, bin_lower_s, bin_upper_s, cycle_count, updated_at)
        SELECT d.bucket,
               d.asset_id,
               floor(i.duration_s / w.bin_s) * w.bin_s           AS bin_lower_s,
               floor(i.duration_s / w.bin_s) * w.bin_s + w.bin_s AS bin_upper_s,
               count(*),
               clock_timestamp()
        FROM (SELECT DISTINCT asset_id, time_bucket(INTERVAL '1 day', start_time) AS bucket FROM _oee_touched) d
        CROSS JOIN LATERAL (
            SELECT coalesce(
                       (SELECT r.histogram_bin_s FROM core.oee_rules r WHERE r.asset_id = d.asset_id),
                       (SELECT r.histogram_bin_s FROM core.oee_rules r WHERE r.asset_id = '*'),
                       1.0) AS bin_s
        ) w
        JOIN mart.event_intervals i
          ON i.asset_id = d.asset_id
         AND i.start_time >= d.bucket
         AND i.start_time <  d.bucket + INTERVAL '1 day'
        WHERE i.stop_class = 'CYCLE' AND i.is_valid
        GROUP BY 1, 2, 3, 4;

        -- 4) Bookkeeping for pipeline-health monitoring.
        INSERT INTO mart.pipeline_state (key, ts_value, details, updated_at)
        VALUES ('pairing_watermark', v_snapshot, '{}'::jsonb, clock_timestamp()),
               ('refresh_cycle_metrics', clock_timestamp(),
                jsonb_build_object(
                    'job_id', job_id,
                    'paired', v_paired,
                    'buckets', v_buckets,
                    'duration_ms', round(extract(epoch FROM clock_timestamp() - v_started) * 1000)),
                clock_timestamp())
        ON CONFLICT (key) DO UPDATE
           SET ts_value = EXCLUDED.ts_value, details = EXCLUDED.details, updated_at = EXCLUDED.updated_at;

    EXCEPTION WHEN OTHERS THEN
        GET STACKED DIAGNOSTICS v_msg = MESSAGE_TEXT, v_state = RETURNED_SQLSTATE, v_ctx = PG_EXCEPTION_CONTEXT;
        INSERT INTO core.error_logs (source, error_type, message, details)
        VALUES ('job:refresh_cycle_metrics', v_state, v_msg,
                jsonb_build_object('job_id', job_id, 'context', v_ctx, 'config', config));
        RAISE WARNING 'refresh_cycle_metrics failed (%): %', v_state, v_msg;
    END;
END
$proc$;

COMMENT ON PROCEDURE mart.refresh_cycle_metrics(integer, jsonb) IS
    'Pairs START/END events via event_uid into mart.event_intervals and refreshes mart.cycle_metrics / mart.cycle_histogram. Errors are logged to core.error_logs.';
