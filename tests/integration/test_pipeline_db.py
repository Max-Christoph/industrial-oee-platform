"""End-to-end SQL behaviour: idempotency, UTC handling, START/END pairing, cycle-time
statistics, stop classification, batch isolation, job error logging, role security."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from ingest_worker.writer import EventWriter
from oee_shared import TelemetryEvent, ValidationLimits

pytestmark = pytest.mark.integration

BASE = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
LIMITS = ValidationLimits(site_timezone="Europe/Berlin")


def ev(uid, state, cat, at, asset="M1", **kw) -> TelemetryEvent:
    return TelemetryEvent.model_validate(
        {"event_uid": uid, "asset_id": asset, "event_state": state, "event_category": cat,
         "event_datetime": at, **kw},
        context={"limits": LIMITS},
    )


def pair(uid, cat, start, seconds, asset="M1"):
    return [ev(uid, "START", cat, start, asset), ev(uid, "END", cat, start + timedelta(seconds=seconds), asset)]


def write(dsn, events, errors=None):
    async def _run():
        w = EventWriter(dsn)
        await w.connect()
        try:
            return await w.write(events, errors)
        finally:
            await w.close()

    if sys.platform == "win32":
        return asyncio.run(_run(), loop_factory=asyncio.SelectorEventLoop)
    return asyncio.run(_run())


def refresh(conn, config: str = "{}"):
    conn.execute("CALL mart.refresh_cycle_metrics(0, %s::jsonb)", (config,))


def test_idempotent_ingest(conn, test_dsn):
    events = pair("c1", "PROCESS", BASE, 10)
    assert write(test_dsn, events).inserted == 2
    second = write(test_dsn, events + events)
    assert (second.inserted, second.duplicates) == (0, 4)
    assert conn.execute("SELECT count(*) FROM core.events").fetchone()[0] == 2


def test_timestamps_stored_as_utc(conn, test_dsn):
    local = (BASE + timedelta(hours=1)).astimezone(LIMITS.tz).replace(tzinfo=None)  # naive site-local
    write(test_dsn, [ev("tz", "INSTANT", "INFO", local.isoformat())])
    conn.execute("SET TIME ZONE 'UTC'")
    stored = conn.execute("SELECT event_datetime FROM core.events WHERE event_uid = 'tz'").fetchone()[0]
    assert stored == BASE + timedelta(hours=1)
    assert conn.execute(
        "SELECT data_type FROM information_schema.columns WHERE table_schema='core' "
        "AND table_name='events' AND column_name='event_datetime'"
    ).fetchone()[0] == "timestamp with time zone"


def test_pairing_and_cycle_statistics(conn, test_dsn):
    durations = [10, 11, 12, 13, 14, 15, 16, 17, 18, 100]
    events = []
    for i, d in enumerate(durations):
        events += pair(f"cyc{i}", "PROCESS", BASE + timedelta(minutes=i), d)
    events += pair("micro", "ERROR", BASE + timedelta(minutes=20), 30)
    events += pair("down", "STANDBY", BASE + timedelta(minutes=30), 900)
    events += pair("setup", "SETUP", BASE + timedelta(minutes=45), 600)
    events += pair("neg", "PROCESS", BASE + timedelta(minutes=50), -5)      # END before START
    events += pair("long", "PROCESS", BASE + timedelta(minutes=52), 5000)   # > max_cycle_s
    events.append(ev("open", "START", "PROCESS", BASE + timedelta(minutes=55)))  # no END yet
    write(test_dsn, events)
    refresh(conn)

    n_intervals = conn.execute("SELECT count(*) FROM mart.event_intervals").fetchone()[0]
    assert n_intervals == len(durations) + 5  # open START is not paired

    m = conn.execute(
        "SELECT cycle_count, cycle_min_s, cycle_max_s, cycle_median_s, cycle_avg_s, cycle_p25_s, cycle_p75_s, "
        "cycle_p95_s, rejected_cycle_count, microstop_count, microstop_s, downtime_count, downtime_s, "
        "setup_count, setup_s FROM mart.cycle_metrics WHERE asset_id = 'M1' AND bucket = %s",
        (BASE,),
    ).fetchone()
    (count, mn, mx, med, avg, p25, p75, p95, rejected, mc, ms, dc, ds, sc, ss) = m
    assert count == 10 and (mn, mx) == (10, 100)
    assert med == pytest.approx(14.5)
    assert avg == pytest.approx(sum(durations) / 10)
    assert p25 == pytest.approx(12.25) and p75 == pytest.approx(16.75)
    assert p95 == pytest.approx(59.5)
    assert rejected == 2
    assert (mc, ms, dc, ds, sc, ss) == (1, 30, 1, 900, 1, 600)

    reasons = dict(conn.execute(
        "SELECT event_uid, invalid_reason FROM mart.event_intervals WHERE NOT is_valid").fetchall())
    assert reasons == {"neg": "non_positive_duration", "long": "above_max_cycle"}

    hist = conn.execute(
        "SELECT sum(cycle_count) FROM mart.cycle_histogram WHERE asset_id = 'M1'").fetchone()[0]
    assert hist == 10

    health = conn.execute("SELECT seconds_since_last_event, seconds_since_last_aggregation, last_paired_count "
                          "FROM mart.v_pipeline_health").fetchone()
    assert health[0] is not None and health[1] < 60 and health[2] == n_intervals


def test_late_end_event_paired_incrementally(conn, test_dsn):
    write(test_dsn, [ev("late", "START", "PROCESS", BASE)])
    refresh(conn)
    assert conn.execute("SELECT count(*) FROM mart.event_intervals").fetchone()[0] == 0
    write(test_dsn, [ev("late", "END", "PROCESS", BASE + timedelta(seconds=42))])
    refresh(conn)
    assert conn.execute("SELECT duration_s FROM mart.event_intervals WHERE event_uid='late'").fetchone()[0] == 42
    refresh(conn)  # re-run is a no-op
    assert conn.execute("SELECT count(*) FROM mart.event_intervals").fetchone()[0] == 1


def test_per_asset_rules_applied(conn, test_dsn):
    conn.execute("INSERT INTO core.oee_rules (asset_id, min_cycle_s, max_cycle_s, microstop_max_s) "
                 "VALUES ('M2', 1, 20, 60)")
    write(test_dsn, pair("r1", "PROCESS", BASE, 25, "M2") + pair("r2", "ERROR", BASE + timedelta(minutes=5), 90, "M2"))
    refresh(conn)
    rows = dict(conn.execute("SELECT event_uid, coalesce(invalid_reason, stop_class) FROM mart.event_intervals "
                             "WHERE asset_id='M2'").fetchall())
    assert rows == {"r1": "above_max_cycle", "r2": "DOWNTIME"}


def test_batch_isolation_with_real_constraints(conn, test_dsn):
    good = ev("g1", "INSTANT", "INFO", BASE)
    poisoned = TelemetryEvent.model_construct(**{**good.model_dump(), "event_uid": "p1", "event_category": "BOGUS"})
    poisoned.__dict__["event_category"] = type("E", (), {"value": "BOGUS"})()
    res = write(test_dsn, [good, poisoned, ev("g2", "INSTANT", "INFO", BASE)])
    assert (res.inserted, res.rejected) == (2, 1)
    assert conn.execute("SELECT count(*) FROM core.events").fetchone()[0] == 2
    assert conn.execute("SELECT error_type FROM core.error_logs").fetchone()[0] == "db_reject"


def test_job_failure_logged_not_raised(conn):
    refresh(conn, '{"lookback": "not an interval"}')
    row = conn.execute("SELECT source, error_type FROM core.error_logs").fetchone()
    assert row == ("job:refresh_cycle_metrics", "22007")


def test_job_registered(conn):
    n = conn.execute("SELECT count(*) FROM timescaledb_information.jobs WHERE proc_name='refresh_cycle_metrics'")
    assert n.fetchone()[0] == 1


def test_hypertable_policies(conn):
    rows = dict(conn.execute(
        "SELECT hypertable_schema || '.' || hypertable_name, proc_name FROM timescaledb_information.jobs "
        "WHERE hypertable_name IN ('events', 'error_logs')").fetchall())
    assert rows == {"core.events": "policy_compression", "core.error_logs": "policy_retention"}


@pytest.mark.parametrize("role", ["grafana_reader", "bi_reader", "ai_reader"])
def test_reader_roles_cannot_write(conn, role):
    conn.execute(f"SET ROLE {role}")
    try:
        with pytest.raises(psycopg.errors.Error):
            with conn.transaction():
                conn.execute("INSERT INTO mart.pipeline_state (key) VALUES ('x')")
    finally:
        conn.execute("RESET ROLE")


def test_reader_roles_have_no_write_privileges(conn):
    rows = conn.execute(
        "SELECT grantee, table_schema, table_name, privilege_type FROM information_schema.role_table_grants "
        "WHERE grantee IN ('grafana_reader','bi_reader','ai_reader') AND privilege_type <> 'SELECT'"
    ).fetchall()
    assert rows == []


def test_ai_reader_settings(conn):
    settings = conn.execute(
        "SELECT unnest(setconfig) FROM pg_db_role_setting s JOIN pg_roles r ON r.oid = s.setrole "
        "WHERE r.rolname = 'ai_reader'").fetchall()
    flat = {s[0] for s in settings}
    assert "default_transaction_read_only=on" in flat and "statement_timeout=3s" in flat


def test_mart_sql_is_idempotent(conn):
    from tests.integration.conftest import REPO

    for f in sorted((REPO / "db" / "mart").glob("*.sql")):
        conn.execute(f.read_text(encoding="utf-8"))
        conn.execute(f.read_text(encoding="utf-8"))
