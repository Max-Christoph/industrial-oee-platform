"""Declarative site configuration: ``oee_rules.yaml``, ``assets.csv``, ``custom_views.sql``.

Resolution order per file (first match wins):
  1. ``$SITE_OVERRIDES_DIR/<file>``  (default ``./site_overrides``)  – your plant, git-ignored
  2. ``$SITE_DEFAULTS_DIR/<file>``   (default ``./examples/demo_plant``) – fictional demo seed
"""

from __future__ import annotations

import csv
import logging
import os
import re
from datetime import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[4]
RULES_FILE = "oee_rules.yaml"
ASSETS_FILE = "assets.csv"
CUSTOM_VIEWS_FILE = "custom_views.sql"


def site_dirs() -> list[Path]:
    overrides = Path(os.environ.get("SITE_OVERRIDES_DIR", REPO_ROOT / "site_overrides"))
    defaults = Path(os.environ.get("SITE_DEFAULTS_DIR", REPO_ROOT / "examples" / "demo_plant"))
    return [overrides, defaults]


def resolve_site_file(name: str, dirs: list[Path] | None = None) -> Path | None:
    for d in dirs or site_dirs():
        candidate = d / name
        if candidate.is_file():
            return candidate
    return None


# --------------------------------------------------------------------------- rules
class AssetRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_cycle_s: float | None = Field(default=None, gt=0)
    max_cycle_s: float | None = Field(default=None, gt=0)
    microstop_max_s: float | None = Field(default=None, ge=0)
    histogram_bin_s: float | None = Field(default=None, gt=0)


class RuleDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_cycle_s: float = Field(default=0.1, gt=0)
    max_cycle_s: float = Field(default=3600.0, gt=0)
    microstop_max_s: float = Field(default=300.0, ge=0)
    histogram_bin_s: float = Field(default=1.0, gt=0)

    def merged(self, rule: AssetRule) -> RuleDefaults:
        data = self.model_dump()
        data.update({k: v for k, v in rule.model_dump().items() if v is not None})
        merged = RuleDefaults(**data)
        if merged.max_cycle_s <= merged.min_cycle_s:
            raise ValueError("max_cycle_s must be greater than min_cycle_s")
        return merged


class MappingRules(BaseModel):
    """Translates plant-specific payload dialects into the generic event schema."""

    model_config = ConfigDict(extra="forbid")

    topic_asset_regex: str | None = None
    field_aliases: dict[str, str] = Field(default_factory=dict)
    category_aliases: dict[str, str] = Field(default_factory=dict)
    state_aliases: dict[str, str] = Field(default_factory=dict)
    asset_aliases: dict[str, str] = Field(default_factory=dict)

    @field_validator("topic_asset_regex")
    @classmethod
    def _valid_regex(cls, v: str | None) -> str | None:
        if v is not None:
            pattern = re.compile(v)
            if "asset_id" not in pattern.groupindex:
                raise ValueError("topic_asset_regex needs a named group (?P<asset_id>...)")
        return v

    @field_validator("category_aliases", "state_aliases")
    @classmethod
    def _upper_keys(cls, v: dict[str, str]) -> dict[str, str]:
        return {str(k).strip().upper(): str(val).strip().upper() for k, val in v.items()}

    def apply(self, raw: dict[str, Any], topic: str | None = None) -> dict[str, Any]:
        data = {self.field_aliases.get(k, k): v for k, v in raw.items()}
        if not data.get("asset_id") and topic and self.topic_asset_regex:
            match = re.match(self.topic_asset_regex, topic)
            if match:
                data["asset_id"] = match.group("asset_id")
        if (asset := data.get("asset_id")) is not None:
            data["asset_id"] = self.asset_aliases.get(str(asset), asset)
        for key, aliases in (("event_category", self.category_aliases), ("event_state", self.state_aliases)):
            value = data.get(key)
            if isinstance(value, str):
                data[key] = aliases.get(value.strip().upper(), value)
        if topic and "source_topic" not in data:
            data["source_topic"] = topic
        return data


WEEKDAYS = {"mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 7}


class ShiftWindow(BaseModel):
    """A recurring planned-production window in local wall-clock time."""

    model_config = ConfigDict(extra="forbid")

    days: list[int] = Field(min_length=1, description="weekdays the shift STARTS on: mon..sun or 1..7")
    start: time
    end: time  # end <= start means the shift ends on the next day
    name: str | None = Field(default=None, max_length=64)

    @field_validator("days", mode="before")
    @classmethod
    def _parse_days(cls, v: Any) -> list[int]:
        items = v if isinstance(v, list) else [v]
        out: list[int] = []
        for item in items:
            if isinstance(item, str) and item.strip().lower()[:3] in WEEKDAYS:
                out.append(WEEKDAYS[item.strip().lower()[:3]])
            elif isinstance(item, int) and not isinstance(item, bool) and 1 <= item <= 7:
                out.append(item)
            else:
                raise ValueError(f"invalid weekday {item!r} (use mon..sun or 1..7)")
        return sorted(set(out))

    @model_validator(mode="after")
    def _non_empty(self) -> ShiftWindow:
        if self.start == self.end:
            raise ValueError("shift start and end must differ (omit planned_time for 24/7)")
        return self


class PlannedTime(BaseModel):
    """Shift model for planned production time. Omitted entirely = 24/7."""

    model_config = ConfigDict(extra="forbid")

    timezone: str | None = None  # default: SITE_TIMEZONE
    default: list[ShiftWindow] = Field(default_factory=list)
    assets: dict[str, list[ShiftWindow]] = Field(default_factory=dict)

    @field_validator("timezone")
    @classmethod
    def _valid_tz(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                ZoneInfo(v)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError(f"unknown time zone {v!r}") from exc
        return v

    def rows(self) -> list[tuple[str, int, time, time, str, str | None]]:
        tz = self.timezone or os.environ.get("SITE_TIMEZONE", "UTC")
        rows: dict[tuple[str, int, time], tuple[str, int, time, time, str, str | None]] = {}
        for asset_id, windows in [("*", self.default), *self.assets.items()]:
            for w in windows:
                for day in w.days:
                    key = (asset_id, day, w.start)
                    if key in rows:
                        raise ValueError(f"duplicate shift start {w.start} on weekday {day} for {asset_id!r}")
                    rows[key] = (asset_id, day, w.start, w.end, tz, w.name)
        return list(rows.values())


class SiteRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    site: dict[str, Any] = Field(default_factory=dict)
    defaults: RuleDefaults = Field(default_factory=RuleDefaults)
    assets: dict[str, AssetRule] = Field(default_factory=dict)
    mapping: MappingRules = Field(default_factory=MappingRules)
    planned_time: PlannedTime | None = None

    @model_validator(mode="after")
    def _check_merge(self) -> SiteRules:
        self.defaults.merged(AssetRule())
        for asset_id, rule in self.assets.items():
            try:
                self.defaults.merged(rule)
            except ValueError as exc:
                raise ValueError(f"rule for asset {asset_id!r}: {exc}") from exc
        if self.planned_time is not None:
            self.planned_time.rows()  # raises on duplicates
        return self

    def shift_rows(self) -> list[tuple[str, int, time, time, str, str | None]]:
        return self.planned_time.rows() if self.planned_time else []

    def effective_rows(self) -> list[tuple[str, float, float, float, float]]:
        rows = [("*", *self._values(self.defaults))]
        rows += [(aid, *self._values(self.defaults.merged(r))) for aid, r in sorted(self.assets.items())]
        return rows

    @staticmethod
    def _values(r: RuleDefaults) -> tuple[float, float, float, float]:
        return (r.min_cycle_s, r.max_cycle_s, r.microstop_max_s, r.histogram_bin_s)


def load_rules(path: Path | None = None) -> SiteRules:
    path = path or resolve_site_file(RULES_FILE)
    if path is None:
        log.warning("no %s found, using built-in defaults", RULES_FILE)
        return SiteRules()
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    log.info("loaded rules from %s", path)
    return SiteRules.model_validate(data)


# --------------------------------------------------------------------------- assets
class AssetRow(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    asset_id: str = Field(min_length=1, max_length=128)
    parent_id: str | None = None
    name: str = Field(min_length=1)
    asset_type: str = "machine"
    ideal_cycle_time_s: float | None = Field(default=None, gt=0)

    @field_validator("parent_id", "ideal_cycle_time_s", mode="before")
    @classmethod
    def _empty_to_none(cls, v: Any) -> Any:
        return None if isinstance(v, str) and not v.strip() else v


def load_assets(path: Path | None = None) -> list[AssetRow]:
    path = path or resolve_site_file(ASSETS_FILE)
    if path is None:
        return []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = [AssetRow.model_validate(r) for r in csv.DictReader(fh) if any((v or "").strip() for v in r.values())]
    ids = {r.asset_id for r in rows}
    if len(ids) != len(rows):
        raise ValueError(f"{path}: duplicate asset_id")
    return rows


def load_custom_views(path: Path | None = None) -> str | None:
    path = path or resolve_site_file(CUSTOM_VIEWS_FILE)
    if path is None:
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text or None


# --------------------------------------------------------------------------- DB sync
async def sync_site_config(conn: Any, rules: SiteRules, assets: list[AssetRow], custom_sql: str | None) -> None:
    """Upsert rules + assets and (re)create site views. ``conn``: psycopg AsyncConnection (autocommit)."""
    async with conn.transaction():
        rows = rules.effective_rows()
        await conn.execute(
            "DELETE FROM core.oee_rules WHERE asset_id <> ALL(%s)", ([r[0] for r in rows],)
        )
        async with conn.cursor() as cur:
            await cur.executemany(
                """INSERT INTO core.oee_rules (asset_id, min_cycle_s, max_cycle_s, microstop_max_s, histogram_bin_s)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (asset_id) DO UPDATE SET
                     min_cycle_s = EXCLUDED.min_cycle_s, max_cycle_s = EXCLUDED.max_cycle_s,
                     microstop_max_s = EXCLUDED.microstop_max_s, histogram_bin_s = EXCLUDED.histogram_bin_s,
                     updated_at = now()""",
                rows,
            )
            # shift model: full replace (no rows = 24/7 planned production)
            await cur.execute("DELETE FROM core.shift_patterns")
            if shifts := rules.shift_rows():
                await cur.executemany(
                    """INSERT INTO core.shift_patterns
                         (asset_id, iso_weekday, start_local, end_local, timezone, shift_name)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    shifts,
                )
            if assets:
                await cur.executemany(
                    """INSERT INTO core.assets (asset_id, parent_id, name, asset_type, ideal_cycle_time_s)
                       VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT (asset_id) DO UPDATE SET
                         parent_id = EXCLUDED.parent_id, name = EXCLUDED.name, asset_type = EXCLUDED.asset_type,
                         ideal_cycle_time_s = EXCLUDED.ideal_cycle_time_s, updated_at = now()""",
                    [(a.asset_id, a.parent_id, a.name, a.asset_type, a.ideal_cycle_time_s) for a in assets],
                )
    log.info(
        "synced %d rule rows, %d shift patterns and %d assets",
        len(rules.effective_rows()), len(rules.shift_rows()), len(assets),
    )

    if custom_sql:
        try:
            async with conn.transaction():
                await conn.execute(custom_sql)
            log.info("applied %s", CUSTOM_VIEWS_FILE)
        except Exception as exc:  # noqa: BLE001 – a broken site view must never stop ingest
            log.error("custom views failed: %s", exc)
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO core.error_logs (source, error_type, message) VALUES (%s, %s, %s)",
                    ("site_config", "custom_views", str(exc)[:2000]),
                )
