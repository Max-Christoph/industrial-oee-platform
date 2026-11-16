"""Async MQTT -> TimescaleDB pipeline (QoS 1, persistent session, micro-batching).

Data flow:  aiomqtt consumer -> bounded asyncio.Queue (back-pressure) -> batch collector
            -> EventWriter (COPY staging -> ON CONFLICT DO NOTHING)

* MQTT reconnects with exponential backoff; the persistent session (clean_session=False,
  fixed client id) lets Mosquitto queue up to ``max_queued_messages`` while we're away.
* DB outages: the current batch is retried until it succeeds (never dropped); while it
  is retried the queue fills up and the consumer stops reading, so the broker buffers.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import ssl
import sys
import time
from pathlib import Path

import aiomqtt
import psycopg

from ingest_worker.backoff import Backoff
from ingest_worker.parser import ParseResult, parse_payload
from ingest_worker.settings import IngestSettings
from ingest_worker.writer import EventWriter
from oee_shared.site_config import load_assets, load_custom_views, load_rules, sync_site_config

log = logging.getLogger("ingest_worker")


def touch(path: str) -> None:
    with contextlib.suppress(OSError):
        Path(path).touch()


async def collect_batch(
    queue: asyncio.Queue[ParseResult], max_events: int, max_wait_s: float
) -> ParseResult | None:
    """Wait up to ``max_wait_s`` for the first item, then gather until size or deadline is hit.

    Returns ``None`` when nothing arrived (idle tick).
    """
    try:
        first = await asyncio.wait_for(queue.get(), timeout=max_wait_s)
    except TimeoutError:
        return None
    batch = ParseResult(events=list(first.events), errors=list(first.errors))
    deadline = time.monotonic() + max_wait_s
    while len(batch.events) < max_events:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            item = await asyncio.wait_for(queue.get(), timeout=remaining)
        except TimeoutError:
            break
        batch.events.extend(item.events)
        batch.errors.extend(item.errors)
    return batch


class IngestService:
    def __init__(self, settings: IngestSettings) -> None:
        self.settings = settings
        self.rules = load_rules()
        self.queue: asyncio.Queue[ParseResult] = asyncio.Queue(maxsize=settings.queue_max_messages)
        self.writer = EventWriter(settings.database_url)
        self.stop = asyncio.Event()
        self.stats = {"received": 0, "inserted": 0, "duplicates": 0, "rejected": 0, "errors": 0}

    # ------------------------------------------------------------------ DB
    async def connect_db(self) -> None:
        backoff = Backoff(self.settings.backoff_initial_s, self.settings.backoff_max_s)
        while not self.stop.is_set():
            try:
                conn = await self.writer.connect()
                log.info("connected to database")
                return conn
            except psycopg.OperationalError as exc:
                delay = backoff.next_delay()
                log.warning("database unavailable (%s) - retry in %.1fs", str(exc).strip(), delay)
                touch(self.settings.heartbeat_file)
                await asyncio.sleep(delay)
        return None

    async def write_with_retry(self, batch: ParseResult) -> None:
        backoff = Backoff(self.settings.backoff_initial_s, self.settings.backoff_max_s)
        while True:
            try:
                res = await self.writer.write(batch.events, batch.errors)
            except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
                delay = backoff.next_delay()
                log.error("database write failed (%s) - retrying batch in %.1fs", str(exc).strip(), delay)
                touch(self.settings.heartbeat_file)
                await asyncio.sleep(delay)
                await self.connect_db()
                continue
            for key in ("inserted", "duplicates", "rejected"):
                self.stats[key] += getattr(res, key)
            self.stats["errors"] += res.errors_logged
            log.debug("batch written: %s", res)
            return

    async def flush_loop(self) -> None:
        max_wait = self.settings.batch_max_wait_ms / 1000
        last_report = time.monotonic()
        while not (self.stop.is_set() and self.queue.empty()):
            batch = await collect_batch(self.queue, self.settings.batch_max_size, max_wait)
            touch(self.settings.heartbeat_file)
            if batch is not None and (batch.events or batch.errors):
                await self.write_with_retry(batch)
            if time.monotonic() - last_report >= 60:
                log.info("stats %s queue=%d", self.stats, self.queue.qsize())
                last_report = time.monotonic()

    # ------------------------------------------------------------------ MQTT
    def _tls_context(self) -> ssl.SSLContext | None:
        if not self.settings.mqtt_tls:
            return None
        ctx = ssl.create_default_context(cafile=self.settings.mqtt_ca_file)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        return ctx

    async def consume_mqtt(self) -> None:
        s = self.settings
        backoff = Backoff(s.backoff_initial_s, s.backoff_max_s)
        while not self.stop.is_set():
            try:
                async with aiomqtt.Client(
                    s.mqtt_host,
                    s.mqtt_port,
                    username=s.mqtt_username,
                    password=s.mqtt_password,
                    identifier=s.mqtt_client_id,
                    clean_session=False,
                    keepalive=s.mqtt_keepalive,
                    tls_context=self._tls_context(),
                    max_inflight_messages=200,
                ) as client:
                    await client.subscribe(s.mqtt_topic, qos=1)
                    log.info("subscribed to %s on %s:%s", s.mqtt_topic, s.mqtt_host, s.mqtt_port)
                    backoff.reset()
                    async for message in client.messages:
                        result = parse_payload(
                            str(message.topic), message.payload, self.rules.mapping, s.limits
                        )
                        self.stats["received"] += 1
                        await self.queue.put(result)
                        if self.stop.is_set():
                            break
            except aiomqtt.MqttError as exc:
                if self.stop.is_set():
                    break
                delay = backoff.next_delay()
                log.warning("MQTT connection lost (%s) - reconnect in %.1fs", exc, delay)
                await asyncio.sleep(delay)

    # ------------------------------------------------------------------ lifecycle
    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        if sys.platform != "win32":
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, self.stop.set)

        conn = await self.connect_db()
        if conn is None:
            return
        try:
            await sync_site_config(conn, self.rules, load_assets(), load_custom_views())
        except Exception:
            log.exception("site configuration sync failed - continuing with existing DB state")

        flusher = asyncio.create_task(self.flush_loop(), name="flush")
        consumer = asyncio.create_task(self.consume_mqtt(), name="mqtt")
        stopper = asyncio.create_task(self.stop.wait(), name="stop")
        done, _ = await asyncio.wait({flusher, consumer, stopper}, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task is not stopper and task.exception():
                log.error("task %s crashed: %r", task.get_name(), task.exception())
        self.stop.set()
        consumer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer
        log.info("draining %d queued messages", self.queue.qsize())
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(flusher, timeout=30)
        await self.writer.close()
        log.info("stopped, final stats %s", self.stats)
