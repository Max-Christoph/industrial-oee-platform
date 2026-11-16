"""TelemetryEvent: timezone normalisation, plausibility filters, schema hygiene."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from oee_shared import EventCategory, EventState, TelemetryEvent, ValidationLimits

BERLIN = ValidationLimits(site_timezone="Europe/Berlin", max_cycle_time_s=3600, max_future_skew_s=300)
NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def make(**overrides):
    data = {
        "event_uid": "c-1",
        "asset_id": "LINE-01-M01",
        "event_state": "START",
        "event_category": "PROCESS",
        "event_datetime": "2026-07-01T10:00:00Z",
    }
    data.update(overrides)
    return TelemetryEvent.model_validate(data, context={"limits": BERLIN, "now": NOW})


class TestTimezoneNormalisation:
    def test_naive_timestamp_is_site_local_summer(self):
        ev = make(event_datetime="2026-07-01T12:00:00")  # CEST = UTC+2
        assert ev.event_datetime == datetime(2026, 7, 1, 10, 0, tzinfo=UTC)

    def test_naive_timestamp_is_site_local_winter(self):
        ev = make(event_datetime="2026-01-15T12:00:00")  # CET = UTC+1
        assert ev.event_datetime == datetime(2026, 1, 15, 11, 0, tzinfo=UTC)

    def test_aware_timestamp_converted_to_utc(self):
        ev = make(event_datetime="2026-07-01T08:00:00-04:00")
        assert ev.event_datetime == datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
        assert ev.event_datetime.tzinfo is UTC

    def test_zulu_suffix(self):
        assert make(event_datetime="2026-07-01T10:00:00.250Z").event_datetime.microsecond == 250_000

    @pytest.mark.parametrize("epoch", [1782900000, 1782900000000, 1782900000.5])
    def test_epoch_seconds_and_millis(self, epoch):
        ev = make(event_datetime=epoch)
        assert ev.event_datetime.tzinfo is UTC
        assert abs(ev.event_datetime.timestamp() - 1782900000) < 1

    def test_dst_gap_does_not_crash(self):
        # 2026-03-29 02:30 does not exist in Europe/Berlin - must still yield an aware UTC value
        assert make(event_datetime="2026-03-29T02:30:00").event_datetime.tzinfo is UTC

    @pytest.mark.parametrize("bad", ["not-a-date", "", True, None, {"x": 1}])
    def test_invalid_timestamps(self, bad):
        with pytest.raises(ValidationError):
            make(event_datetime=bad)

    def test_future_events_rejected(self):
        with pytest.raises(ValidationError, match="future"):
            make(event_datetime=(NOW + timedelta(hours=1)).isoformat())

    def test_small_clock_skew_accepted(self):
        assert make(event_datetime=(NOW + timedelta(seconds=60)).isoformat())


class TestCycleTimePlausibility:
    @pytest.mark.parametrize("value", [-1, 0, -0.001, float("nan"), float("inf"), 3601])
    def test_rejects_negative_zero_and_implausible(self, value):
        with pytest.raises(ValidationError, match="cycle_time_s"):
            make(cycle_time_s=value)

    @pytest.mark.parametrize("value", [0.01, 12.5, 3600])
    def test_accepts_plausible(self, value):
        assert make(cycle_time_s=value).cycle_time_s == value


class TestSchema:
    def test_enums_case_insensitive(self):
        ev = make(event_state="end", event_category=" error ")
        assert ev.event_state is EventState.END
        assert ev.event_category is EventCategory.ERROR

    def test_unknown_category_rejected(self):
        with pytest.raises(ValidationError):
            make(event_category="LUNCH")

    def test_extra_fields_preserved_in_attributes(self):
        ev = make(operator="A. Smith", attributes={"shift": 2})
        assert ev.attributes == {"shift": 2, "operator": "A. Smith"}

    def test_numeric_ids_coerced_to_str(self):
        ev = make(event_uid=4711, asset_id=7)
        assert (ev.event_uid, ev.asset_id) == ("4711", "7")

    def test_empty_uid_rejected(self):
        with pytest.raises(ValidationError):
            make(event_uid="  ")

    def test_to_row_matches_columns(self):
        from oee_shared import EVENT_COLUMNS

        row = make(cycle_time_s=10).to_row()
        assert len(row) == len(EVENT_COLUMNS)
        assert row[2] == "START" and row[4] == "PROCESS"
        assert row[7] == "{}"

    def test_default_state_instant(self):
        data = {"event_uid": "i", "asset_id": "a", "event_category": "INFO", "event_datetime": NOW}
        ev = TelemetryEvent.model_validate(data, context={"limits": BERLIN, "now": NOW})
        assert ev.event_state is EventState.INSTANT
