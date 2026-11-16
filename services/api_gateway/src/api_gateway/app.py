"""Read-only REST gateway on top of the standardised ``mart`` BI views.

Pure transport: every KPI is computed in SQL (db/mart/); this service only filters,
paginates and serialises. Authentication via ``X-API-Key`` (comma-separated
``API_KEYS``), compared in constant time.
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, Protocol

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Security, status
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from oee_shared.logging_setup import setup_logging

log = logging.getLogger("api_gateway")

MAX_LIMIT = 10_000


class ApiSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    database_url: str = "postgresql://bi_reader@localhost:5432/oee?sslmode=prefer"
    api_keys: str = Field(default="", description="comma-separated list of valid API keys")
    pool_min_size: int = 1
    pool_max_size: int = 10

    @property
    def key_list(self) -> list[str]:
        return [k.strip() for k in self.api_keys.split(",") if len(k.strip()) >= 16]


class Database(Protocol):
    async def fetch(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...


class PoolDatabase:
    def __init__(self, dsn: str, min_size: int, max_size: int) -> None:
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool

        self.pool = AsyncConnectionPool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            open=False,
            kwargs={"row_factory": dict_row, "autocommit": True, "application_name": "oee-api-gateway"},
        )

    async def fetch(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, params or {})
            return await cur.fetchall()


api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def get_db(request: Request) -> Database:
    return request.app.state.db


def require_api_key(request: Request, key: Annotated[str | None, Security(api_key_header)]) -> str:
    valid: list[str] = request.app.state.api_keys
    if not key or not any(hmac.compare_digest(key.encode(), k.encode()) for k in valid):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing API key",
            headers={"WWW-Authenticate": "APIKey"},
        )
    return key


DB = Annotated[Database, Depends(get_db)]


def _window(start: datetime | None, end: datetime | None, default: timedelta) -> tuple[datetime, datetime]:
    end = end or datetime.now(UTC)
    start = start or end - default
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if start >= end:
        raise HTTPException(status_code=422, detail="start must be before end")
    return start, end


def create_app(settings: ApiSettings | None = None, db: Database | None = None) -> FastAPI:
    settings = settings or ApiSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        setup_logging("api_gateway")
        if not app.state.api_keys:
            log.warning("no valid API_KEYS configured (min. 16 chars) - all /v1 requests will be rejected")
        pool_db = None
        if app.state.db is None:
            pool_db = PoolDatabase(settings.database_url, settings.pool_min_size, settings.pool_max_size)
            await pool_db.pool.open(wait=False)
            app.state.db = pool_db
        yield
        if pool_db is not None:
            await pool_db.pool.close()

    app = FastAPI(
        title="industrial-oee-platform API",
        version="0.1.0",
        description="Read-only access to standardised OEE/cycle-time views (schema `mart`).",
        lifespan=lifespan,
    )
    app.state.db = db
    app.state.api_keys = settings.key_list

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error: %s", exc)
        return JSONResponse(status_code=500, content={"detail": "internal error"})

    @app.get("/health", tags=["ops"])
    async def health(db: DB) -> JSONResponse:
        try:
            await db.fetch("SELECT 1 AS ok")
        except Exception as exc:  # noqa: BLE001
            log.warning("health check failed: %s", exc)
            return JSONResponse(status_code=503, content={"status": "degraded", "database": "unreachable"})
        return JSONResponse(content={"status": "ok", "database": "ok"})

    v1 = [Depends(require_api_key)]

    @app.get("/v1/assets", tags=["master data"], dependencies=v1)
    async def assets(db: DB) -> list[dict[str, Any]]:
        return await db.fetch("SELECT * FROM mart.v_asset_hierarchy ORDER BY path")

    @app.get("/v1/pipeline-health", tags=["ops"], dependencies=v1)
    async def pipeline_health(db: DB) -> dict[str, Any]:
        rows = await db.fetch("SELECT * FROM mart.v_pipeline_health")
        return rows[0] if rows else {}

    @app.get("/v1/cycle-metrics", tags=["kpi"], dependencies=v1)
    async def cycle_metrics(
        db: DB,
        asset_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        granularity: Literal["hour", "day"] = "hour",
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 1000,
    ) -> list[dict[str, Any]]:
        start, end = _window(start, end, timedelta(days=1) if granularity == "hour" else timedelta(days=30))
        view = "mart.v_cycle_metrics_hourly" if granularity == "hour" else "mart.v_cycle_metrics_daily"
        return await db.fetch(
            f"SELECT * FROM {view} WHERE bucket >= %(start)s AND bucket < %(end)s "
            "AND (%(asset_id)s::text IS NULL OR asset_id = %(asset_id)s) "
            "ORDER BY bucket, asset_id LIMIT %(limit)s",
            {"start": start, "end": end, "asset_id": asset_id, "limit": limit},
        )

    @app.get("/v1/oee", tags=["kpi"], dependencies=v1)
    async def oee(
        db: DB,
        asset_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        granularity: Literal["hour", "day"] = "hour",
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 1000,
    ) -> list[dict[str, Any]]:
        """Availability, performance, quality and OEE (ratios 0..1) - computed in SQL (mart.v_oee_*)."""
        start, end = _window(start, end, timedelta(days=1) if granularity == "hour" else timedelta(days=30))
        view = "mart.v_oee_hourly" if granularity == "hour" else "mart.v_oee_daily"
        return await db.fetch(
            f"SELECT * FROM {view} WHERE bucket >= %(start)s AND bucket < %(end)s "
            "AND (%(asset_id)s::text IS NULL OR asset_id = %(asset_id)s) "
            "ORDER BY bucket, asset_id LIMIT %(limit)s",
            {"start": start, "end": end, "asset_id": asset_id, "limit": limit},
        )

    @app.get("/v1/cycle-histogram", tags=["kpi"], dependencies=v1)
    async def cycle_histogram(
        db: DB,
        asset_id: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 5000,
    ) -> list[dict[str, Any]]:
        start, end = _window(start, end, timedelta(days=7))
        return await db.fetch(
            "SELECT * FROM mart.v_cycle_histogram WHERE asset_id = %(asset_id)s "
            "AND bucket >= %(start)s AND bucket < %(end)s ORDER BY bucket, bin_lower_s LIMIT %(limit)s",
            {"start": start, "end": end, "asset_id": asset_id, "limit": limit},
        )

    @app.get("/v1/stops", tags=["kpi"], dependencies=v1)
    async def stops(
        db: DB,
        asset_id: str | None = None,
        stop_class: Literal["MICROSTOP", "DOWNTIME", "SETUP"] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 1000,
    ) -> list[dict[str, Any]]:
        start, end = _window(start, end, timedelta(days=1))
        return await db.fetch(
            "SELECT * FROM mart.v_stop_events WHERE start_time >= %(start)s AND start_time < %(end)s "
            "AND (%(asset_id)s::text IS NULL OR asset_id = %(asset_id)s) "
            "AND (%(stop_class)s::text IS NULL OR stop_class = %(stop_class)s) "
            "ORDER BY start_time DESC LIMIT %(limit)s",
            {"start": start, "end": end, "asset_id": asset_id, "stop_class": stop_class, "limit": limit},
        )

    return app


app = create_app()
