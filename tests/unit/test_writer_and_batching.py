"""Batch failure isolation (writer) and micro-batch collection (service)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from psycopg import errors as pg_errors

from ingest_worker.parser import ParseResult
from ingest_worker.service import collect_batch
from ingest_worker.writer import EventWriter
from oee_shared import ErrorRecord, TelemetryEvent, ValidationLimits

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def event(uid: str) -> TelemetryEvent:
    return TelemetryEvent.model_validate(
        {"event_uid": uid, "asset_id": "A", "event_state": "START", "event_category": "PROCESS",
         "event_datetime": "2026-07-01T10:00:00Z"},
        context={"limits": ValidationLimits(), "now": NOW},
    )


class FakeWriter(EventWriter):
    """Simulates the DB: rejects the whole batch if it contains a poisoned row."""

    def __init__(self, poisoned: set[str], existing: set[str] | None = None) -> None:
        super().__init__("postgresql://unused")
        self.poisoned, self.stored = poisoned, set(existing or ())
        self.logged: list[ErrorRecord] = []
        self.batch_calls = 0

    async def _write_batch(self, events):
        self.batch_calls += 1
        if any(e.event_uid in self.poisoned for e in events):
            raise pg_errors.CheckViolation("violates check constraint")
        return self._store(events)

    async def _write_single(self, event):
        if event.event_uid in self.poisoned:
            raise pg_errors.InvalidTextRepresentation("bad value")
        return self._store([event])

    async def _write_errors(self, errors):
        self.logged.extend(errors)

    def _store(self, events):
        new = {e.event_uid for e in events} - self.stored
        self.stored |= new
        return len(new)


async def test_clean_batch_single_round_trip():
    w = FakeWriter(poisoned=set())
    res = await w.write([event("a"), event("b")])
    assert (res.inserted, res.rejected, res.duplicates, w.batch_calls) == (2, 0, 0, 1)


async def test_poisoned_row_is_isolated_and_logged():
    w = FakeWriter(poisoned={"bad"})
    res = await w.write([event("a"), event("bad"), event("c")])
    assert res.inserted == 2 and res.rejected == 1
    assert w.stored == {"a", "c"}
    assert [e.error_type for e in w.logged] == ["db_reject"]
    assert "bad" in w.logged[0].payload


async def test_duplicates_counted_not_rejected():
    w = FakeWriter(poisoned=set(), existing={"a"})
    res = await w.write([event("a"), event("b")])
    assert (res.inserted, res.duplicates) == (1, 1)


async def test_parse_errors_written_even_without_events():
    w = FakeWriter(poisoned=set())
    err = ErrorRecord(source="ingest_worker", error_type="json_decode", message="x")
    res = await w.write([], [err])
    assert res.errors_logged == 1 and w.logged == [err] and w.batch_calls == 0


async def test_connection_errors_propagate_for_retry():
    class DownWriter(FakeWriter):
        async def _write_batch(self, events):
            raise pg_errors.AdminShutdown("terminating connection")

    with pytest.raises(pg_errors.AdminShutdown):
        await DownWriter(poisoned=set()).write([event("a")])


async def test_collect_batch_respects_size_and_idle():
    q: asyncio.Queue[ParseResult] = asyncio.Queue()
    assert await collect_batch(q, max_events=10, max_wait_s=0.02) is None
    for i in range(5):
        q.put_nowait(ParseResult(events=[event(f"{i}a"), event(f"{i}b")]))
    batch = await collect_batch(q, max_events=4, max_wait_s=0.5)
    assert len(batch.events) == 4
    rest = await collect_batch(q, max_events=100, max_wait_s=0.05)
    assert len(rest.events) == 6


async def test_collect_batch_carries_errors():
    q: asyncio.Queue[ParseResult] = asyncio.Queue()
    q.put_nowait(ParseResult(errors=[ErrorRecord("s", "t", "m")]))
    batch = await collect_batch(q, max_events=10, max_wait_s=0.05)
    assert len(batch.errors) == 1 and not batch.events


def test_backoff_bounded():
    from ingest_worker.backoff import Backoff

    b = Backoff(1, 8)
    delays = [b.next_delay() for _ in range(10)]
    assert all(0.5 <= d <= 8 for d in delays) and max(delays) > 4
    b.reset()
    assert b.next_delay() <= 1
