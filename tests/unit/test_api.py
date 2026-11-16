"""API gateway: X-API-Key auth, read-only view access, parameter validation."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from api_gateway.app import ApiSettings, create_app

KEY = "k" * 32


class FakeDB:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def fetch(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if self.fail:
            raise ConnectionError("db down")
        self.calls.append((sql, params))
        if "v_pipeline_health" in sql:
            return [{"seconds_since_last_event": 3.2, "seconds_since_last_aggregation": 41.0}]
        return [{"asset_id": "LINE-01-M01", "cycle_count": 10}]


@pytest.fixture
def db() -> FakeDB:
    return FakeDB()


@pytest.fixture
def client(db: FakeDB) -> TestClient:
    app = create_app(ApiSettings(api_keys=f"{KEY}, short, {'z' * 20}"), db=db)
    with TestClient(app) as c:
        yield c


ENDPOINTS = ["/v1/assets", "/v1/pipeline-health", "/v1/cycle-metrics", "/v1/stops", "/v1/oee",
             "/v1/cycle-histogram?asset_id=A"]


@pytest.mark.parametrize("path", ENDPOINTS)
def test_missing_key_rejected(client, path):
    r = client.get(path)
    assert r.status_code == 401


@pytest.mark.parametrize("path", ENDPOINTS)
def test_wrong_key_rejected(client, path):
    assert client.get(path, headers={"X-API-Key": "x" * 32}).status_code == 401


def test_short_keys_from_config_are_ignored(client):
    assert client.get("/v1/assets", headers={"X-API-Key": "short"}).status_code == 401


@pytest.mark.parametrize("key", [KEY, "z" * 20])
def test_valid_keys(client, key):
    assert client.get("/v1/assets", headers={"X-API-Key": key}).status_code == 200


def test_health_is_public(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["database"] == "ok"


def test_health_degraded_when_db_down():
    app = create_app(ApiSettings(api_keys=KEY), db=FakeDB(fail=True))
    with TestClient(app) as c:
        assert c.get("/health").status_code == 503


def test_cycle_metrics_queries_only_mart_with_params(client, db):
    r = client.get("/v1/cycle-metrics", params={"asset_id": "LINE-01-M01", "granularity": "day"},
                   headers={"X-API-Key": KEY})
    assert r.status_code == 200
    sql, params = db.calls[-1]
    assert "FROM mart.v_cycle_metrics_daily" in sql
    assert params["asset_id"] == "LINE-01-M01"
    assert "LINE-01-M01" not in sql  # parameterised, never interpolated


def test_invalid_window_rejected(client):
    r = client.get("/v1/stops", params={"start": "2026-02-01T00:00:00Z", "end": "2026-01-01T00:00:00Z"},
                   headers={"X-API-Key": KEY})
    assert r.status_code == 422


def test_limit_bounds(client):
    r = client.get("/v1/cycle-metrics", params={"limit": 100_000}, headers={"X-API-Key": KEY})
    assert r.status_code == 422


def test_invalid_stop_class(client):
    r = client.get("/v1/stops", params={"stop_class": "DROP TABLE"}, headers={"X-API-Key": KEY})
    assert r.status_code == 422


def test_no_write_methods(client):
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)("/v1/assets", headers={"X-API-Key": KEY}).status_code == 405


@pytest.mark.parametrize("granularity, view", [("hour", "mart.v_oee_hourly"), ("day", "mart.v_oee_daily")])
def test_oee_is_a_plain_select_on_the_sql_view(client, db, granularity, view):
    r = client.get("/v1/oee", params={"asset_id": "LINE-01-M01", "granularity": granularity},
                   headers={"X-API-Key": KEY})
    assert r.status_code == 200
    sql, params = db.calls[-1]
    assert f"FROM {view}" in sql and params["asset_id"] == "LINE-01-M01"
