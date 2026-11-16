-- Registers the native TimescaleDB background job (procedure: db/mart/20_refresh_cycle_metrics.sql).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM timescaledb_information.jobs
        WHERE proc_schema = 'mart' AND proc_name = 'refresh_cycle_metrics'
    ) THEN
        PERFORM add_job(
            'mart.refresh_cycle_metrics'::regproc,
            INTERVAL '1 minute',
            config => '{"lookback": "7 days", "overlap": "5 minutes"}'::jsonb);
    END IF;
END
$$;
