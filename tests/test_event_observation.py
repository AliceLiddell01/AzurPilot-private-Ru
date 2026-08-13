from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from module.event_datamine.artifact import load_builtin_artifact
from module.webui.event_observation import (
    dashboard_pt_observation,
    empty_event_observation,
    event_observation_path,
    load_event_observation,
    observation_is_fresh,
    save_event_observation,
)
from module.webui.event_source import empty_event_user_state, event_plan_from_source


def test_observation_round_trip_is_event_server_and_profile_scoped(tmp_path: Path):
    root = tmp_path / "observations"
    observation = empty_event_observation("en:5941", "EN", "alpha")
    observation.update(
        {
            "observed_at": "2026-08-13T10:00:00+00:00",
            "source": "dashboard_ocr",
            "current_pt": 1234,
        }
    )
    save_event_observation("alpha", observation, root=root)

    assert (
        load_event_observation("alpha", "en:5941", "EN", root=root)["current_pt"]
        == 1234
    )
    assert (
        load_event_observation("beta", "en:5941", "EN", root=root)["current_pt"] is None
    )
    assert (
        load_event_observation("alpha", "en:other", "EN", root=root)["current_pt"]
        is None
    )
    assert event_observation_path(
        "alpha", "en:5941", "EN", root
    ) != event_observation_path("alpha", "en:5941", "JP", root)


def test_fixture_and_replay_cannot_become_production_truth(tmp_path: Path):
    observation = empty_event_observation("en:5941", "EN", "alpha")
    observation.update(
        {
            "source": "fixture",
            "observed_at": "2026-08-13T10:00:00+00:00",
            "current_pt": 999,
        }
    )
    with pytest.raises(ValueError, match="Fixture/replay"):
        save_event_observation("alpha", observation, root=tmp_path)

    save_event_observation(
        "alpha", observation, root=tmp_path, allow_nonproduction=True
    )
    production = load_event_observation("alpha", "en:5941", "EN", root=tmp_path)
    assert production["current_pt"] is None
    assert production["findings"][0]["code"] == "nonproduction_evidence_rejected"


def test_save_rejects_missing_identity_and_cross_profile_before_write(tmp_path: Path):
    missing_identity = empty_event_observation("", "", "alpha")
    with pytest.raises(ValueError, match="event_id и server"):
        save_event_observation("alpha", missing_identity, root=tmp_path)

    cross_profile = empty_event_observation("en:5941", "EN", "beta")
    with pytest.raises(ValueError, match="другому профилю"):
        save_event_observation("alpha", cross_profile, root=tmp_path)

    assert list(tmp_path.rglob("*.json")) == []


def test_missing_or_stale_dashboard_pt_stays_unknown_not_zero():
    now = datetime(2026, 8, 13, 12, tzinfo=timezone.utc)
    missing = dashboard_pt_observation(
        instance="ap",
        event_id="en:5941",
        server="EN",
        value=None,
        recorded_at="",
        now=now,
    )
    stale = dashboard_pt_observation(
        instance="ap",
        event_id="en:5941",
        server="EN",
        value=42,
        recorded_at=(now - timedelta(hours=49)).isoformat(),
        now=now,
    )

    assert missing["current_pt"] is None
    assert not observation_is_fresh(missing, now=now)
    assert stale["current_pt"] == 42
    assert not observation_is_fresh(stale, now=now)

    plan = event_plan_from_source(
        load_builtin_artifact()["event_spec"], empty_event_user_state(), stale
    )
    assert plan["progress"]["current_pt"] is None
    assert plan["progress"]["status"] == "stale"


def test_corrupt_observation_is_backed_up_before_fail_closed_fallback(tmp_path: Path):
    path = event_observation_path("ap", "en:5941", "EN", tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('{"event_id":', encoding="utf-8")

    result = load_event_observation("ap", "en:5941", "EN", root=tmp_path)

    assert result["current_pt"] is None
    assert not path.exists()
    assert len(list(path.parent.glob(f"{path.name}.corrupt-*"))) == 1
