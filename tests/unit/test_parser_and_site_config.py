"""Payload parsing (mapping + per-element error isolation) and site config resolution."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ingest_worker.parser import parse_payload
from oee_shared import ValidationLimits
from oee_shared.site_config import (
    MappingRules,
    SiteRules,
    load_assets,
    load_custom_views,
    load_rules,
    resolve_site_file,
)

LIMITS = ValidationLimits(site_timezone="Europe/Berlin")
NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
TOPIC = "oee/demo/LINE-01-M01/events"


@pytest.fixture(scope="module")
def mapping() -> MappingRules:
    return load_rules().mapping


def ev(**kw):
    base = {
        "event_uid": "u1",
        "event_state": "START",
        "event_category": "PROCESS",
        "event_datetime": "2026-07-01T10:00:00Z",
    }
    base.update(kw)
    return base


class TestParser:
    def test_valid_single_object_takes_asset_from_topic(self, mapping):
        res = parse_payload(TOPIC, json.dumps(ev()).encode(), mapping, LIMITS, NOW)
        assert not res.errors
        assert res.events[0].asset_id == "LINE-01-M01"
        assert res.events[0].source_topic == TOPIC

    def test_invalid_json_goes_to_errors(self, mapping):
        res = parse_payload(TOPIC, b"{not json", mapping, LIMITS, NOW)
        assert not res.events
        assert res.errors[0].error_type == "json_decode"
        assert res.errors[0].payload == "{not json"

    def test_non_utf8_payload(self, mapping):
        res = parse_payload(TOPIC, b"\xff\xfe\x00", mapping, LIMITS, NOW)
        assert res.errors[0].error_type == "json_decode"

    def test_batch_with_bad_element_keeps_valid_ones(self, mapping):
        payload = [ev(event_uid="ok1"), ev(event_uid="bad", cycle_time_s=-5), "garbage", ev(event_uid="ok2")]
        res = parse_payload(TOPIC, json.dumps(payload), mapping, LIMITS, NOW)
        assert [e.event_uid for e in res.events] == ["ok1", "ok2"]
        assert len(res.errors) == 2
        assert {e.details["index"] for e in res.errors} == {1, 2}
        assert "cycle_time_s" in res.errors[0].message

    def test_plant_dialect_is_mapped(self, mapping):
        raw = {"uid": "x", "asset": "PLC_PRESS_A", "state": "finished", "category": "Fault",
               "ts": "2026-07-01T12:00:00"}
        res = parse_payload("plc/raw", json.dumps(raw), mapping, LIMITS, NOW)
        assert not res.errors, res.errors
        e = res.events[0]
        assert (e.asset_id, e.event_state.value, e.event_category.value) == ("LINE-01-M01", "END", "ERROR")
        assert e.event_datetime == datetime(2026, 7, 1, 10, 0, tzinfo=UTC)  # local CEST -> UTC

    def test_missing_asset_is_an_error(self, mapping):
        res = parse_payload("other/topic", json.dumps(ev()), mapping, LIMITS, NOW)
        assert not res.events and res.errors[0].error_type == "validation"

    def test_payload_truncated_in_error_log(self, mapping):
        res = parse_payload(TOPIC, b"x" * 100_000, mapping, LIMITS, NOW)
        assert len(res.errors[0].payload) == 4096


class TestSiteConfig:
    def test_demo_plant_is_default(self, repo_root):
        assert resolve_site_file("oee_rules.yaml") == repo_root / "examples" / "demo_plant" / "oee_rules.yaml"

    def test_override_wins(self, tmp_path, repo_root):
        (tmp_path / "assets.csv").write_text("asset_id,name\nX,Y\n")
        found = resolve_site_file("assets.csv", [tmp_path, repo_root / "examples" / "demo_plant"])
        assert found == tmp_path / "assets.csv"

    def test_rules_merge_defaults(self):
        rows = {r[0]: r for r in load_rules().effective_rows()}
        assert rows["*"] == ("*", 1.0, 600.0, 300.0, 1.0)
        assert rows["LINE-01-M01"] == ("LINE-01-M01", 1.0, 120.0, 300.0, 1.0)
        assert rows["LINE-02-M01"][4] == 5

    def test_inconsistent_rule_rejected(self):
        with pytest.raises(ValidationError, match="max_cycle_s"):
            SiteRules.model_validate({"assets": {"A": {"min_cycle_s": 50, "max_cycle_s": 10}}})

    def test_regex_requires_named_group(self):
        with pytest.raises(ValidationError):
            MappingRules(topic_asset_regex="^oee/(.*)$")

    def test_assets_csv(self):
        assets = {a.asset_id: a for a in load_assets()}
        assert assets["DEMO-PLANT"].parent_id is None
        assert assets["LINE-01-M01"].parent_id == "LINE-01"
        assert assets["LINE-01-M01"].ideal_cycle_time_s == 12.0

    def test_duplicate_assets_rejected(self, tmp_path):
        p = tmp_path / "assets.csv"
        p.write_text("asset_id,name\nA,1\nA,2\n")
        with pytest.raises(ValueError, match="duplicate"):
            load_assets(p)

    def test_custom_views_only_create_or_replace(self):
        sql = load_custom_views()
        assert sql and "CREATE OR REPLACE VIEW site." in sql
        assert "DROP " not in sql.upper()
