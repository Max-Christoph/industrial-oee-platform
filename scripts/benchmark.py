#!/usr/bin/env python3
"""Sustained-ingest benchmark: MQTT (QoS 1) -> ingest_worker -> TimescaleDB.

For each target rate (events/s) the benchmark publishes realistic START/END pairs
(shared event_uid, plausible cycle times) for ``--duration`` seconds, waits until the
pipeline has drained and then verifies in the database:

  * data loss         - every published event must be stored exactly once (0 % loss)
  * end-to-end latency - received_at (DB) minus sent_at (publisher) -> p50 / p95 / p99
  * batch throughput  - stored events/s, number of COPY batches, mean batch size

Run via ``make bench`` (executes inside the ingest_worker image on oee_net) or locally:
    uv run scripts/benchmark.py --mqtt-host localhost --mqtt-port 8883 --tls --ca site_overrides/certs/ca.crt \
        --dsn postgresql://postgres:...@localhost:5432/oee?sslmode=require
Exit code 0 = all rates passed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import ssl
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import aiomqtt
import psycopg

from oee_shared import EventCategory, EventState, TelemetryEvent, ValidationLimits

ASSETS = [f"BENCH-{i:02d}" for i in range(1, 11)]


@dataclass
class RateResult:
    rate: int
    published: int
    stored: int
    publish_rate: float
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    throughput: float | None
    batches: int
    avg_batch: float | None

    @property
    def loss_pct(self) -> float:
        return 0.0 if self.published == 0 else 100.0 * (self.published - self.stored) / self.published

    @property
    def passed(self) -> bool:
        return self.stored == self.published and self.publish_rate >= 0.95 * self.rate


def make_pair(run_id: str, seq: int) -> tuple[str, str]:
    """Build a START/END pair validated through the shared schema (same code path as ingest)."""
    asset = ASSETS[seq % len(ASSETS)]
    cycle = max(0.5, random.gauss(12.0, 1.5))  # noqa: S311
    end = datetime.now(UTC)
    start = end - timedelta(seconds=cycle)
    uid = f"{run_id}-{seq:08d}"
    limits = ValidationLimits()
    out = []
    for state, ts in ((EventState.START, start), (EventState.END, end)):
        ev = TelemetryEvent.model_validate(
            {
                "event_uid": uid,
                "asset_id": asset,
                "event_state": state,
                "event_category": EventCategory.PROCESS,
                "event_datetime": ts,
                "cycle_time_s": round(cycle, 3) if state is EventState.END else None,
                "attributes": {"bench_run": run_id, "sent_at": time.time()},
            },
            context={"limits": limits},
        )
        out.append(ev.model_dump_json(exclude_none=True))
    return out[0], out[1]


async def publish_rate(client: aiomqtt.Client, run_id: str, rate: int, duration: float) -> tuple[int, float]:
    topic = "oee/bench/{asset}/events"
    sent, seq = 0, 0
    total = int(rate * duration)
    t0 = time.perf_counter()
    while sent < total:
        due = min(total, int((time.perf_counter() - t0) * rate))
        tasks = []
        while sent + 2 <= max(due, 2) and sent < total:
            start_msg, end_msg = make_pair(run_id, seq)
            asset = ASSETS[seq % len(ASSETS)]
            tasks.append(client.publish(topic.format(asset=asset), start_msg, qos=1))
            tasks.append(client.publish(topic.format(asset=asset), end_msg, qos=1))
            sent += 2
            seq += 1
        if tasks:
            await asyncio.gather(*tasks)
        await asyncio.sleep(0.005)
    elapsed = time.perf_counter() - t0
    return sent, sent / elapsed if elapsed > 0 else 0.0


def verify(dsn: str, run_id: str, expected: int, since: datetime, timeout: float) -> tuple[int, dict]:
    deadline = time.monotonic() + timeout
    with psycopg.connect(dsn, autocommit=True) as conn:
        stored = 0
        while time.monotonic() < deadline:
            stored = conn.execute(
                "SELECT count(*) FROM core.events WHERE event_datetime >= %s AND event_uid LIKE %s",
                (since, f"{run_id}-%"),
            ).fetchone()[0]
            if stored >= expected:
                break
            time.sleep(1.0)
        stats = conn.execute(
            """
            SELECT percentile_cont(ARRAY[0.5, 0.95, 0.99]) WITHIN GROUP (ORDER BY lat_ms) AS pct,
                   count(DISTINCT received_at) AS batches,
                   extract(epoch FROM max(received_at) - min(received_at)) AS span_s,
                   count(*) AS n
            FROM (
                SELECT received_at,
                       1000 * (extract(epoch FROM received_at) - (attributes ->> 'sent_at')::double precision) AS lat_ms
                FROM core.events
                WHERE event_datetime >= %s AND event_uid LIKE %s
            ) s
            """,
            (since, f"{run_id}-%"),
        ).fetchone()
    pct, batches, span_s, n = stats
    return stored, {"pct": pct, "batches": batches or 0, "span_s": float(span_s or 0), "n": n}


def cleanup(dsn: str, since: datetime) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "DELETE FROM core.events WHERE event_datetime >= %s AND asset_id LIKE 'BENCH-%%'", (since,)
        )
        conn.execute("DELETE FROM mart.event_intervals WHERE asset_id LIKE 'BENCH-%%'")
        conn.execute("DELETE FROM mart.cycle_metrics WHERE asset_id LIKE 'BENCH-%%'")
        conn.execute("DELETE FROM mart.cycle_histogram WHERE asset_id LIKE 'BENCH-%%'")


async def run(args: argparse.Namespace) -> int:
    tls_context = None
    if args.tls:
        tls_context = ssl.create_default_context(cafile=args.ca)
        if args.insecure:
            tls_context.check_hostname = False
            tls_context.verify_mode = ssl.CERT_NONE
    results: list[RateResult] = []
    since = datetime.now(UTC) - timedelta(minutes=5)
    async with aiomqtt.Client(
        args.mqtt_host,
        args.mqtt_port,
        username=args.mqtt_username,
        password=args.mqtt_password,
        identifier=f"oee-bench-{uuid.uuid4().hex[:6]}",
        tls_context=tls_context,
        max_inflight_messages=1000,
        max_queued_outgoing_messages=100_000,
    ) as client:
        for rate in args.rates:
            run_id = f"bench{uuid.uuid4().hex[:8]}r{rate}"
            print(f"-> {rate} events/s for {args.duration:.0f}s (run {run_id}) ...", flush=True)
            published, achieved = await publish_rate(client, run_id, rate, args.duration)
            stored, st = await asyncio.to_thread(verify, args.dsn, run_id, published, since, args.drain_timeout)
            pct = st["pct"] or [None, None, None]
            results.append(
                RateResult(
                    rate=rate,
                    published=published,
                    stored=stored,
                    publish_rate=achieved,
                    p50_ms=pct[0],
                    p95_ms=pct[1],
                    p99_ms=pct[2],
                    throughput=(st["n"] / st["span_s"]) if st["span_s"] > 0 else None,
                    batches=st["batches"],
                    avg_batch=(st["n"] / st["batches"]) if st["batches"] else None,
                )
            )

    def f(v: float | None, fmt: str = "{:8.1f}") -> str:
        return fmt.format(v) if v is not None else "     n/a"

    print()
    print(
        " target | published | stored | loss % | pub ev/s | p50 ms | p95 ms | p99 ms |"
        " ingest ev/s | batches | avg batch | result"
    )
    print("-" * 125)
    for r in results:
        print(
            f" {r.rate:6d} | {r.published:9d} | {r.stored:6d} | {r.loss_pct:6.2f} | {r.publish_rate:8.1f} |"
            f"{f(r.p50_ms)}|{f(r.p95_ms)}|{f(r.p99_ms)}| {f(r.throughput, '{:11.1f}')} | {r.batches:7d} |"
            f" {f(r.avg_batch, '{:9.1f}')} | {'PASS' if r.passed else 'FAIL'}"
        )
    if args.json:
        print(json.dumps([r.__dict__ | {"loss_pct": r.loss_pct, "passed": r.passed} for r in results], default=str))
    if args.cleanup:
        await asyncio.to_thread(cleanup, args.dsn, since)
        print("benchmark data removed.")
    return 0 if all(r.passed for r in results) else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--rates", default=os.environ.get("BENCH_RATES", "100,250,500"),
                   type=lambda s: [int(x) for x in s.split(",") if x.strip()])
    p.add_argument("--duration", type=float, default=float(os.environ.get("BENCH_DURATION", "30")))
    p.add_argument("--drain-timeout", type=float, default=60.0)
    p.add_argument("--mqtt-host", default=os.environ.get("MQTT_HOST", "localhost"))
    p.add_argument("--mqtt-port", type=int, default=int(os.environ.get("MQTT_PORT", "1883")))
    p.add_argument("--mqtt-username", default=os.environ.get("BENCH_MQTT_USERNAME"))
    p.add_argument("--mqtt-password", default=os.environ.get("BENCH_MQTT_PASSWORD"))
    p.add_argument("--tls", action="store_true")
    p.add_argument("--ca", default=None)
    p.add_argument("--insecure", action="store_true", help="skip TLS hostname/CA verification")
    p.add_argument("--dsn", default=os.environ.get("BENCH_ADMIN_DSN"))
    p.add_argument("--keep", dest="cleanup", action="store_false", help="keep benchmark rows in the DB")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()
    if not args.dsn:
        p.error("--dsn or BENCH_ADMIN_DSN required")
    if sys.platform == "win32":
        return asyncio.run(run(args), loop_factory=asyncio.SelectorEventLoop)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
