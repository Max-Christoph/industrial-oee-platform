"""OEE inputs on the transport side: quantity validation and the shift model (no OEE maths here -
that lives in SQL, see tests/integration/test_pipeline_db.py::test_oee_*)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, time

import pytest
from pydantic import ValidationError

from ingest_worker.parser import parse_payload
from oee_shared import EVENT_COLUMNS, TelemetryEvent, ValidationLimits
from oee_shared.site_config import SiteRules, load_rules

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
LIMITS = ValidationLimits(site_timezone="Europe/Berlin")


def make(**kw):
    data = {"event_uid": "c1", "asset_id": "A", "event_state": "END", "event_category": "PROCESS",
            "event_datetime": "2026-07-01T10:00:00Z", **kw}
    return TelemetryEvent.model_validate(data, context={"limits": LIMITS, "now": NOW})


class TestQuantities:
    def test_quantities_stored_in_attributes(self):
        row = dict(zip(EVENT_COLUMNS, make(quantity_good=4, quantity_scrap=1, attributes={"x": 1}).to_row(),
                       strict=True))
        assert json.loads(row["attributes"]) == {"x": 1, "quantity_good": 4, "quantity_scrap": 1}

    def test_no_quantities_no_keys(self):
        row = dict(zip(EVENT_COLUMNS, make().to_row(), strict=True))
        assert json.loads(row["attributes"]) == {}

    @pytest.mark.parametrize("bad", [-1, float("nan"), float("inf"), 2e9, "viele"])
    def test_invalid_quantities_rejected(self, bad):
        with pytest.raises(ValidationError):
            make(quantity_good=bad)

    def test_fractional_quantities_allowed(self):
        assert make(quantity_good=12.5).quantity_good == 12.5  # e.g. kg or metres

    def test_plant_aliases_for_quantities(self):
        mapping = load_rules().mapping
        raw = {"uid": "u", "state": "END", "category": "RUN", "ts": "2026-07-01T10:00:00Z", "good": 3, "scrap": 1}
        res = parse_payload("oee/demo/LINE-01-M01/events", json.dumps(raw), mapping, LIMITS, NOW)
        assert not res.errors, res.errors
        assert (res.events[0].quantity_good, res.events[0].quantity_scrap) == (3, 1)


class TestShiftModel:
    def rules(self, planned):
        return SiteRules.model_validate({"planned_time": planned})

    def test_no_planned_time_means_24_7(self):
        assert SiteRules().shift_rows() == []
        assert load_rules().shift_rows() == []  # demo plant ships with the example commented out

    def test_rows_expand_days_and_assets(self):
        rows = self.rules({
            "timezone": "Europe/Berlin",
            "default": [{"days": ["mon", "Tuesday", 3], "start": "06:00", "end": "14:00", "name": "early"}],
            "assets": {"M1": [{"days": ["sat"], "start": "22:00", "end": "06:00"}]},
        }).shift_rows()
        assert ("*", 1, time(6), time(14), "Europe/Berlin", "early") in rows
        assert ("*", 3, time(6), time(14), "Europe/Berlin", "early") in rows
        assert ("M1", 6, time(22), time(6), "Europe/Berlin", None) in rows
        assert len(rows) == 4

    def test_timezone_defaults_to_site_timezone(self, monkeypatch):
        monkeypatch.setenv("SITE_TIMEZONE", "America/Chicago")
        rows = self.rules({"default": [{"days": ["mon"], "start": "06:00", "end": "14:00"}]}).shift_rows()
        assert rows[0][4] == "America/Chicago"

    @pytest.mark.parametrize("planned, match", [
        ({"timezone": "Mars/Olympus", "default": []}, "time zone"),
        ({"default": [{"days": ["xyz"], "start": "06:00", "end": "14:00"}]}, "weekday"),
        ({"default": [{"days": [8], "start": "06:00", "end": "14:00"}]}, "weekday"),
        ({"default": [{"days": ["mon"], "start": "06:00", "end": "06:00"}]}, "differ"),
        ({"default": [{"days": ["mon"], "start": "06:00", "end": "14:00"},
                      {"days": ["mon"], "start": "06:00", "end": "10:00"}]}, "duplicate"),
        ({"default": [{"days": ["mon"], "start": "25:00", "end": "14:00"}]}, "time"),
    ])
    def test_invalid_shift_models(self, planned, match):
        with pytest.raises(ValidationError, match=match):
            self.rules(planned)
