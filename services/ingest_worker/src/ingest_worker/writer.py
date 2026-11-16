"""Micro-batch writer: COPY -> temp staging table -> INSERT ... ON CONFLICT DO NOTHING.

Failure isolation:
  * data/constraint errors in a batch -> the batch is retried row by row, poisoned
    rows go to core.error_logs, all valid rows are kept;
  * connection errors (DB down) propagate so the caller can back off and retry the
    *same* batch (no data loss).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import psycopg
from psycopg import errors as pg_errors

from oee_shared import EVENT_COLUMNS, ErrorRecord, TelemetryEvent

log = logging.getLogger(__name__)

_COLS = ", ".join(EVENT_COLUMNS)
STAGING_DDL = (
    "CREATE TEMP TABLE IF NOT EXISTS staging_events "
    "(LIKE core.events INCLUDING DEFAULTS) ON COMMIT DELETE ROWS"
)
COPY_SQL = f"COPY staging_events ({_COLS}) FROM STDIN"
MERGE_SQL = f"INSERT INTO core.events ({_COLS}) SELECT {_COLS} FROM staging_events ON CONFLICT DO NOTHING"
SINGLE_SQL = (
    f"INSERT INTO core.events ({_COLS}) VALUES ({', '.join(['%s'] * len(EVENT_COLUMNS))}) "
    "ON CONFLICT DO NOTHING"
)
ERROR_SQL = (
    "INSERT INTO core.error_logs (source, error_type, message, topic, payload, details) "
    "VALUES (%s, %s, %s, %s, %s, %s::jsonb)"
)

# Errors caused by the *content* of a row (isolate & log) vs. infrastructure errors (retry).
ROW_LEVEL_ERRORS: tuple[type[Exception], ...] = (pg_errors.DataError, pg_errors.IntegrityError)


@dataclass(slots=True)
class WriteResult:
    inserted: int = 0
    duplicates: int = 0
    rejected: int = 0
    errors_logged: int = 0


class EventWriter:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn: psycopg.AsyncConnection | None = None

    @property
    def conn(self) -> psycopg.AsyncConnection:
        if self._conn is None or self._conn.closed:
            raise psycopg.OperationalError("not connected")
        return self._conn

    async def connect(self) -> psycopg.AsyncConnection:
        await self.close()
        self._conn = await psycopg.AsyncConnection.connect(
            self._dsn, autocommit=True, application_name="oee-ingest-worker"
        )
        await self._conn.execute(STAGING_DDL)
        return self._conn

    async def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            await self._conn.close()
        self._conn = None

    # -- overridable primitives (unit-tested via subclassing) -----------------
    async def _write_batch(self, events: list[TelemetryEvent]) -> int:
        conn = self.conn
        async with conn.transaction(), conn.cursor() as cur:
            async with cur.copy(COPY_SQL) as copy:
                for event in events:
                    await copy.write_row(event.to_row())
            await cur.execute(MERGE_SQL)
            return max(cur.rowcount, 0)

    async def _write_single(self, event: TelemetryEvent) -> int:
        conn = self.conn
        async with conn.transaction(), conn.cursor() as cur:
            await cur.execute(SINGLE_SQL, event.to_row())
            return max(cur.rowcount, 0)

    async def _write_errors(self, errors: list[ErrorRecord]) -> None:
        conn = self.conn
        async with conn.transaction(), conn.cursor() as cur:
            await cur.executemany(ERROR_SQL, [e.to_row() for e in errors])

    # -- orchestration --------------------------------------------------------
    async def write(self, events: list[TelemetryEvent], errors: list[ErrorRecord] | None = None) -> WriteResult:
        result = WriteResult()
        errors = list(errors or [])
        if events:
            try:
                result.inserted = await self._write_batch(events)
            except ROW_LEVEL_ERRORS as exc:
                log.warning("batch of %d rejected (%s) - isolating row by row", len(events), exc)
                for event in events:
                    try:
                        result.inserted += await self._write_single(event)
                    except ROW_LEVEL_ERRORS as row_exc:
                        result.rejected += 1
                        errors.append(
                            ErrorRecord(
                                source="ingest_worker",
                                error_type="db_reject",
                                message=str(row_exc).strip(),
                                topic=event.source_topic,
                                payload=ErrorRecord.truncate_payload(
                                    json.dumps(dict(zip(EVENT_COLUMNS, event.to_row(), strict=True)), default=str)
                                ),
                                details={"sqlstate": getattr(row_exc, "sqlstate", None)},
                            )
                        )
            result.duplicates = len(events) - result.inserted - result.rejected
        if errors:
            await self._write_errors(errors)
            result.errors_logged = len(errors)
        return result
