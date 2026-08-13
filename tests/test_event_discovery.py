from datetime import datetime

import pytest

from module.event_datamine.discovery import (
    EventDiscoveryError,
    discover_major_events,
    resolve_current_candidate,
)


def _time(start, end):
    def part(value):
        return {
            0: {0: value.year, 1: value.month, 2: value.day},
            1: {0: value.hour, 1: value.minute, 2: value.second},
        }

    return {0: "always", 1: part(start), 2: part(end)}


def _activity(row_id, mark, kind, maps, start, end):
    return {
        "id": row_id,
        "mark": mark,
        "type": kind,
        "config_data": {index: value for index, value in enumerate(maps)},
        "time": _time(start, end),
    }


class FakeSource:
    class Snapshot:
        server = "EN"

    snapshot = Snapshot()

    def __init__(self, activities, chapters, memories=None):
        self.tables = {
            "activity_template": activities,
            "chapter_template": chapters,
            "memory_group": memories or {},
            "activity_medal_group": {},
        }

    def load_table(self, name):
        return self.tables[name]


def _two_event_source():
    old_start = datetime(2025, 5, 20)
    old_end = datetime(2025, 6, 11, 23, 59, 59)
    new_start = datetime(2026, 8, 13)
    new_end = datetime(2026, 8, 26, 23, 59, 59)
    activities = {
        10: _activity(10, 100, 12, [1001], old_start, old_end),
        11: _activity(11, 100, 14, [8001], old_start, old_end),
        20: _activity(20, 200, 12, [2001], new_start, new_end),
        21: _activity(21, 200, 14, [8002], new_start, datetime(2026, 9, 3)),
        99: _activity(99, 999, 14, [9999], new_start, new_end),
    }
    memories = {
        1: {"link_event": 10, "title": "Historical"},
        2: {"link_event": 20, "title": "Current"},
    }
    return FakeSource(activities, {1001: {}, 2001: {}}, memories)


def test_same_discovery_pipeline_selects_historical_and_current_by_lifecycle():
    candidates = discover_major_events(_two_event_source())

    old = resolve_current_candidate(
        candidates, server="EN", now=datetime(2025, 5, 25)
    )
    new = resolve_current_candidate(
        candidates, server="EN", now=datetime(2026, 8, 20)
    )

    assert old and old.name == "Historical"
    assert new and new.name == "Current"
    assert 99 not in new.related_activity_ids


def test_discovery_returns_none_when_no_active_or_redemption_event():
    candidates = discover_major_events(_two_event_source())
    assert (
        resolve_current_candidate(
            candidates, server="EN", now=datetime(2030, 1, 1)
        )
        is None
    )


def test_multiple_active_events_fail_closed():
    source = _two_event_source()
    source.tables["activity_template"][30] = _activity(
        30,
        300,
        12,
        [3001],
        datetime(2026, 8, 1),
        datetime(2026, 8, 30),
    )
    source.tables["chapter_template"][3001] = {}
    source.tables["memory_group"][3] = {"link_event": 30, "title": "Other"}

    with pytest.raises(EventDiscoveryError) as caught:
        resolve_current_candidate(
            discover_major_events(source),
            server="EN",
            now=datetime(2026, 8, 20),
        )
    assert caught.value.code == "ambiguous_active_event"


def test_ambiguous_campaign_root_is_visible_but_unsupported():
    start = datetime(2026, 8, 1)
    end = datetime(2026, 8, 30)
    activities = {
        40: _activity(40, 400, 12, [4001], start, end),
        41: _activity(41, 400, 12, [4002], start, end),
    }
    candidate = discover_major_events(
        FakeSource(activities, {4001: {}, 4002: {}})
    )[0]
    assert not candidate.supported
    with pytest.raises(EventDiscoveryError) as caught:
        resolve_current_candidate(
            (candidate,), server="EN", now=datetime(2026, 8, 20)
        )
    assert caught.value.code == "ambiguous_campaign_root"


def test_malformed_dates_do_not_become_candidates():
    row = _activity(
        50,
        500,
        12,
        [5001],
        datetime(2026, 8, 1),
        datetime(2026, 8, 30),
    )
    row["time"] = {0: "broken"}
    assert discover_major_events(FakeSource({50: row}, {5001: {}})) == ()
