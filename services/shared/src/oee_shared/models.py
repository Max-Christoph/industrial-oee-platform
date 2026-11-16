"""Unified telemetry event schema shared by ingest_worker, benchmark and tests.

Every field name and enum value is generic English. Plant-specific spellings
(e.g. German state names, PLC field names) are translated *before* validation by
:class:`oee_shared.site_config.MappingRules` from ``oee_rules.yaml``.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

MAX_PAYLOAD_LOG_BYTES = 4096

EVENT_COLUMNS: tuple[str, ...] = (
    "event_datetime",
    "event_uid",
    "event_state",
    "asset_id",
    "event_category",
    "event_code",
    "cycle_time_s",
    "attributes",
    "source_topic",
)


class EventState(StrEnum):
    START = "START"
    END = "END"
    INSTANT = "INSTANT"


class EventCategory(StrEnum):
    PROCESS = "PROCESS"
    ERROR = "ERROR"
    SETUP = "SETUP"
    STANDBY = "STANDBY"
    INFO = "INFO"
    WARNING = "WARNING"


@dataclass(frozen=True, slots=True)
class ValidationLimits:
    """Plausibility limits applied during validation (overridable via context)."""

    site_timezone: str = "UTC"
    max_cycle_time_s: float = 86_400.0
    max_future_skew_s: float = 300.0

    @property
    def tz(self) -> ZoneInfo:
        return _zone(self.site_timezone)


@lru_cache(maxsize=32)
def _zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def limits_from_env() -> ValidationLimits:
    return ValidationLimits(
        site_timezone=os.environ.get("SITE_TIMEZONE", "UTC"),
        max_cycle_time_s=float(os.environ.get("MAX_CYCLE_TIME_S", "86400")),
        max_future_skew_s=float(os.environ.get("MAX_FUTURE_SKEW_S", "300")),
    )


def _limits(info: ValidationInfo) -> ValidationLimits:
    ctx = info.context or {}
    limits = ctx.get("limits")
    return limits if isinstance(limits, ValidationLimits) else limits_from_env()


def normalize_datetime(value: Any, tz: ZoneInfo) -> datetime:
    """Convert ISO strings, epoch seconds/milliseconds or datetimes into aware UTC datetimes.

    Naive timestamps are interpreted in the *site* timezone (PLCs usually send local
    wall-clock time) and converted to UTC.
    """
    if isinstance(value, bool):
        raise ValueError("boolean is not a timestamp")
    if isinstance(value, int | float):
        if not math.isfinite(value):
            raise ValueError("timestamp must be finite")
        seconds = value / 1000.0 if abs(value) > 1e11 else float(value)  # ms vs s heuristic
        return datetime.fromtimestamp(seconds, tz=UTC)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("empty timestamp")
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"invalid ISO-8601 timestamp: {value!r}") from exc
    elif isinstance(value, datetime):
        dt = value
    else:
        raise ValueError(f"unsupported timestamp type: {type(value).__name__}")

    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(UTC)


class TelemetryEvent(BaseModel):
    """A single machine event. START/END pairs share the same ``event_uid``."""

    model_config = ConfigDict(str_strip_whitespace=True, frozen=True, extra="forbid")

    event_uid: str = Field(min_length=1, max_length=128)
    asset_id: str = Field(min_length=1, max_length=128)
    event_state: EventState = EventState.INSTANT
    event_category: EventCategory
    event_datetime: datetime
    event_code: str | None = Field(default=None, max_length=128)
    cycle_time_s: float | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    source_topic: str | None = Field(default=None, max_length=512)

    @model_validator(mode="before")
    @classmethod
    def _collect_extra_fields(cls, data: Any) -> Any:
        """Unknown top-level keys are preserved in ``attributes`` instead of being dropped."""
        if not isinstance(data, dict):
            return data
        known = set(cls.model_fields)
        extra = {k: v for k, v in data.items() if k not in known}
        if not extra:
            return data
        cleaned = {k: v for k, v in data.items() if k in known}
        attrs = dict(cleaned.get("attributes") or {})
        attrs.update(extra)
        cleaned["attributes"] = attrs
        return cleaned

    @field_validator("event_state", "event_category", mode="before")
    @classmethod
    def _upper_enum(cls, v: Any) -> Any:
        return v.strip().upper() if isinstance(v, str) else v

    @field_validator("event_uid", "asset_id", "event_code", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> Any:
        if isinstance(v, bool):
            raise ValueError("boolean is not a valid identifier")
        return str(v) if isinstance(v, int) else v

    @field_validator("event_datetime", mode="before")
    @classmethod
    def _normalize_tz(cls, v: Any, info: ValidationInfo) -> datetime:
        return normalize_datetime(v, _limits(info).tz)

    @field_validator("event_datetime")
    @classmethod
    def _not_in_future(cls, v: datetime, info: ValidationInfo) -> datetime:
        limits = _limits(info)
        now = (info.context or {}).get("now") or datetime.now(UTC)
        if v > now + timedelta(seconds=limits.max_future_skew_s):
            skew = limits.max_future_skew_s
            raise ValueError(f"event_datetime {v.isoformat()} is more than {skew:.0f}s in the future")
        return v

    @field_validator("cycle_time_s")
    @classmethod
    def _plausible_cycle_time(cls, v: float | None, info: ValidationInfo) -> float | None:
        if v is None:
            return v
        if not math.isfinite(v):
            raise ValueError("cycle_time_s must be finite")
        if v <= 0:
            raise ValueError(f"cycle_time_s must be > 0 (got {v})")
        limit = _limits(info).max_cycle_time_s
        if v > limit:
            raise ValueError(f"cycle_time_s {v} exceeds plausibility limit {limit}")
        return v

    @field_validator("attributes")
    @classmethod
    def _json_serializable(cls, v: dict[str, Any]) -> dict[str, Any]:
        try:
            json.dumps(v, allow_nan=False, default=str)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"attributes not JSON serializable: {exc}") from exc
        return v

    def to_row(self) -> tuple[Any, ...]:
        """Row in :data:`EVENT_COLUMNS` order (attributes as JSON text) for COPY."""
        return (
            self.event_datetime,
            self.event_uid,
            self.event_state.value,
            self.asset_id,
            self.event_category.value,
            self.event_code,
            self.cycle_time_s,
            json.dumps(self.attributes, separators=(",", ":"), default=str),
            self.source_topic,
        )


@dataclass(slots=True)
class ErrorRecord:
    """A rejected payload or processing failure destined for ``core.error_logs``."""

    source: str
    error_type: str
    message: str
    topic: str | None = None
    payload: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def truncate_payload(raw: bytes | str | None) -> str | None:
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw[:MAX_PAYLOAD_LOG_BYTES].decode("utf-8", errors="replace")
        else:
            raw = raw[:MAX_PAYLOAD_LOG_BYTES]
        return raw.replace("\x00", "\\0")

    def to_row(self) -> tuple[Any, ...]:
        return (
            self.source,
            self.error_type,
            self.message[:2000],
            self.topic,
            self.payload,
            json.dumps(self.details, default=str),
        )
