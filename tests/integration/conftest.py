"""Integration fixtures: a throw-away database built from db/init/*.sql + db/mart/*.sql.

Requires a reachable TimescaleDB superuser connection, e.g.
    OEE_TEST_DSN=postgresql://postgres:<pw>@localhost:5432/postgres?sslmode=require
Tests are skipped when OEE_TEST_DSN is not set.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

REPO = Path(__file__).resolve().parents[2]
ADMIN_DSN = os.environ.get("OEE_TEST_DSN")

pytestmark = pytest.mark.integration


def sql_files() -> list[Path]:
    init = sorted(p for p in (REPO / "db" / "init").glob("*.sql"))
    mart = sorted((REPO / "db" / "mart").glob("*.sql"))
    # same order as the container init: roles/core/mart tables -> mart objects -> jobs -> security
    before = [p for p in init if p.name < "25"]
    after = [p for p in init if p.name >= "25"]
    return before + mart + after


@pytest.fixture(scope="session")
def test_dsn() -> Iterator[str]:
    if not ADMIN_DSN:
        pytest.skip("OEE_TEST_DSN not set - skipping DB integration tests")
    name = f"oee_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    params = conninfo_to_dict(ADMIN_DSN)
    params["dbname"] = name
    dsn = make_conninfo(**params)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for f in sql_files():
                conn.execute(f.read_text(encoding="utf-8"))
            # no background scheduler noise during tests - procedures are called explicitly
            conn.execute(
                "SELECT alter_job(job_id, scheduled => false) FROM timescaledb_information.jobs "
                "WHERE proc_schema = 'mart'"
            )
        yield dsn
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def conn(test_dsn: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(test_dsn, autocommit=True) as c:
        for table in ("core.events", "core.error_logs", "mart.event_intervals", "mart.cycle_metrics",
                      "mart.cycle_histogram", "mart.pipeline_state"):
            c.execute(f"TRUNCATE {table}")
        c.execute("DELETE FROM core.oee_rules WHERE asset_id <> '*'")
        c.execute("DELETE FROM core.shift_patterns")
        yield c
