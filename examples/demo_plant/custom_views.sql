-- Optional BI alias views for the (fictional) demo plant.
-- Applied by ingest_worker at startup (role oee_app) into schema `site`.
-- Rules: CREATE OR REPLACE VIEW only, build on mart.* views, never on raw core tables.

CREATE OR REPLACE VIEW site.cycle_kpis_daily AS
SELECT bucket            AS day_utc,
       asset_path        AS asset,
       cycle_count       AS cycles,
       cycle_median_s    AS median_cycle_s,
       cycle_p95_s       AS p95_cycle_s,
       microstop_count   AS microstops,
       round((downtime_s / 60.0)::numeric, 1) AS downtime_min
FROM mart.v_cycle_metrics_daily;
