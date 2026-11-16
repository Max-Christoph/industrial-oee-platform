-- KPI: OEE = Availability x Performance x Quality (per asset and hour / day)
-- Idempotent: safe to re-apply any time via `make apply-sql`.
--
--   planned_s     planned production time in the bucket (core.shift_patterns; none = 24/7)
--   run_s         planned_s - downtime_s - setup_s           (microstops count as performance loss)
--   availability  run_s / planned_s
--   performance   ideal_cycle_time_s * total_quantity / run_s (ideal time from core.assets)
--   quality       good_quantity / total_quantity
--   oee           availability * performance * quality       (all ratios 0..1, NULL if undefined)
--
-- Known simplifications (documented, intentionally simple):
--   * stops/cycles are attributed to the bucket in which they START;
--   * hours in which an asset sent no events at all have no row (no data != no production).

-- Planned production seconds of an asset inside [p_from, p_to).
CREATE OR REPLACE FUNCTION mart.planned_seconds(p_asset_id text, p_from timestamptz, p_to timestamptz)
RETURNS double precision
LANGUAGE sql
STABLE
PARALLEL SAFE
-- read-only lookup of the shift model on behalf of readers without access to `core`
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $fn$
    WITH patterns AS (
        -- asset-specific patterns win over the '*' default
        SELECT s.*
        FROM core.shift_patterns s
        WHERE s.asset_id = CASE
            WHEN EXISTS (SELECT 1 FROM core.shift_patterns x WHERE x.asset_id = p_asset_id) THEN p_asset_id
            ELSE '*' END
    ), windows AS (
        SELECT (d.day + p.start_local) AT TIME ZONE p.timezone AS win_start,
               (d.day + p.end_local
                  + CASE WHEN p.end_local <= p.start_local THEN INTERVAL '1 day' ELSE INTERVAL '0' END
               ) AT TIME ZONE p.timezone AS win_end
        FROM patterns p
        -- local calendar days touching the range (incl. the day before for overnight shifts)
        CROSS JOIN LATERAL generate_series(
            0, (p_to AT TIME ZONE p.timezone)::date - (p_from AT TIME ZONE p.timezone)::date + 1) AS g(i)
        CROSS JOIN LATERAL (SELECT (p_from AT TIME ZONE p.timezone)::date - 1 + g.i AS day) d
        WHERE extract(isodow FROM d.day) = p.iso_weekday
    )
    SELECT CASE
        WHEN NOT EXISTS (SELECT 1 FROM patterns) THEN extract(epoch FROM p_to - p_from)
        ELSE least(
            coalesce(sum(greatest(extract(epoch FROM least(w.win_end, p_to) - greatest(w.win_start, p_from)), 0)), 0),
            extract(epoch FROM p_to - p_from))
    END::double precision
    FROM windows w
$fn$;

COMMENT ON FUNCTION mart.planned_seconds(text, timestamptz, timestamptz) IS
    'Planned production seconds of an asset in [from, to) from core.shift_patterns (no patterns = 24/7).';

CREATE OR REPLACE VIEW mart.v_oee_hourly AS
WITH base AS (
    SELECT m.bucket,
           m.asset_id,
           h.name AS asset_name,
           h.path AS asset_path,
           h.ideal_cycle_time_s,
           mart.planned_seconds(m.asset_id, m.bucket, m.bucket + INTERVAL '1 hour') AS planned_s,
           m.downtime_s,
           m.setup_s,
           m.microstop_s,
           m.cycle_count,
           m.good_quantity,
           m.scrap_quantity,
           m.good_quantity + m.scrap_quantity AS total_quantity
    FROM mart.cycle_metrics m
    LEFT JOIN mart.v_asset_hierarchy h USING (asset_id)
), timed AS (
    SELECT b.*, greatest(b.planned_s - b.downtime_s - b.setup_s, 0) AS run_s
    FROM base b
), ratios AS (
    SELECT t.*,
           t.run_s / NULLIF(t.planned_s, 0)                                AS availability,
           t.ideal_cycle_time_s * t.total_quantity / NULLIF(t.run_s, 0)    AS performance,
           t.good_quantity / NULLIF(t.total_quantity, 0)                   AS quality
    FROM timed t
)
SELECT r.*, r.availability * r.performance * r.quality AS oee
FROM ratios r;

COMMENT ON VIEW mart.v_oee_hourly IS
    'OEE per asset and UTC hour: availability, performance, quality (ratios 0..1) plus the underlying times/quantities.';

CREATE OR REPLACE VIEW mart.v_oee_daily AS
WITH sums AS (
    SELECT time_bucket(INTERVAL '1 day', bucket) AS bucket,
           asset_id,
           max(asset_name)         AS asset_name,
           max(asset_path)         AS asset_path,
           max(ideal_cycle_time_s) AS ideal_cycle_time_s,
           sum(planned_s)          AS planned_s,
           sum(run_s)              AS run_s,
           sum(downtime_s)         AS downtime_s,
           sum(setup_s)            AS setup_s,
           sum(microstop_s)        AS microstop_s,
           sum(cycle_count)        AS cycle_count,
           sum(good_quantity)      AS good_quantity,
           sum(scrap_quantity)     AS scrap_quantity,
           sum(total_quantity)     AS total_quantity
    FROM mart.v_oee_hourly
    GROUP BY 1, 2
), ratios AS (
    -- ratios of sums (never averages of hourly ratios)
    SELECT s.*,
           s.run_s / NULLIF(s.planned_s, 0)                                AS availability,
           s.ideal_cycle_time_s * s.total_quantity / NULLIF(s.run_s, 0)    AS performance,
           s.good_quantity / NULLIF(s.total_quantity, 0)                   AS quality
    FROM sums s
)
SELECT r.*, r.availability * r.performance * r.quality AS oee
FROM ratios r;

COMMENT ON VIEW mart.v_oee_daily IS
    'OEE per asset and UTC day, computed from summed times/quantities of mart.v_oee_hourly.';

-- OEE over an arbitrary range and asset selection (ratios of sums; performance weighted
-- by each asset's ideal cycle time). p_assets NULL or containing 'All' = all assets.
CREATE OR REPLACE FUNCTION mart.oee_summary(p_from timestamptz, p_to timestamptz, p_assets text[] DEFAULT NULL)
RETURNS TABLE (
    planned_s      double precision,
    run_s          double precision,
    good_quantity  double precision,
    scrap_quantity double precision,
    availability   double precision,
    performance    double precision,
    quality        double precision,
    oee            double precision
)
LANGUAGE sql
STABLE
AS $fn$
    WITH s AS (
        SELECT sum(h.planned_s)                              AS planned_s,
               sum(h.run_s)                                  AS run_s,
               sum(h.good_quantity)                          AS good_quantity,
               sum(h.scrap_quantity)                         AS scrap_quantity,
               sum(h.total_quantity)                         AS total_quantity,
               sum(h.ideal_cycle_time_s * h.total_quantity)  AS ideal_s
        FROM mart.v_oee_hourly h
        WHERE h.bucket >= p_from AND h.bucket < p_to
          AND (p_assets IS NULL OR 'All' = ANY (p_assets) OR h.asset_id = ANY (p_assets))
    ), r AS (
        SELECT s.*,
               s.run_s / NULLIF(s.planned_s, 0)             AS availability,
               s.ideal_s / NULLIF(s.run_s, 0)               AS performance,
               s.good_quantity / NULLIF(s.total_quantity, 0) AS quality
        FROM s
    )
    SELECT r.planned_s, r.run_s, r.good_quantity, r.scrap_quantity,
           r.availability, r.performance, r.quality,
           r.availability * r.performance * r.quality
    FROM r
$fn$;

COMMENT ON FUNCTION mart.oee_summary(timestamptz, timestamptz, text[]) IS
    'OEE (availability, performance, quality) over a time range and asset selection, as ratios of sums.';
