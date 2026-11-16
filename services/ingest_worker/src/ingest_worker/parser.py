"""MQTT payload -> validated TelemetryEvents + isolated ErrorRecords.

A payload is a JSON object or a JSON array of objects. Every element is validated
individually, so one bad element never discards its valid neighbours.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from oee_shared import ErrorRecord, TelemetryEvent, ValidationLimits
from oee_shared.site_config import MappingRules

SOURCE = "ingest_worker"
MAX_EVENTS_PER_MESSAGE = 10_000


@dataclass(slots=True)
class ParseResult:
    events: list[TelemetryEvent] = field(default_factory=list)
    errors: list[ErrorRecord] = field(default_factory=list)


def _error(error_type: str, message: str, topic: str, raw: bytes | str, **details: Any) -> ErrorRecord:
    return ErrorRecord(
        source=SOURCE,
        error_type=error_type,
        message=message,
        topic=topic,
        payload=ErrorRecord.truncate_payload(raw),
        details=details,
    )


def parse_payload(
    topic: str,
    payload: bytes | bytearray | str,
    mapping: MappingRules,
    limits: ValidationLimits,
    now: datetime | None = None,
) -> ParseResult:
    result = ParseResult()
    raw = bytes(payload) if isinstance(payload, bytearray) else payload
    try:
        doc = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        result.errors.append(_error("json_decode", f"invalid JSON: {exc}", topic, raw))
        return result

    items = doc if isinstance(doc, list) else [doc]
    if len(items) > MAX_EVENTS_PER_MESSAGE:
        result.errors.append(_error("too_many_events", f"{len(items)} events in one message", topic, raw))
        return result

    ctx: dict[str, Any] = {"limits": limits}
    if now is not None:
        ctx["now"] = now
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            result.errors.append(
                _error("validation", "event must be a JSON object", topic, json.dumps(item, default=str), index=index)
            )
            continue
        try:
            mapped = mapping.apply(item, topic)
            result.events.append(TelemetryEvent.model_validate(mapped, context=ctx))
        except ValidationError as exc:
            problems = [f"{'.'.join(map(str, e['loc'])) or '<root>'}: {e['msg']}" for e in exc.errors()]
            result.errors.append(
                _error("validation", "; ".join(problems), topic, json.dumps(item, default=str), index=index)
            )
    return result
