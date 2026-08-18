from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from module.event_datamine.artifact import load_builtin_artifact
from module.webui import event_source
from module.webui.event_observation import (
    dashboard_pt_observation,
    empty_event_observation,
    event_observation_path,
    load_event_observation,
    observation_is_fresh,
    save_event_observation,
)
from module.webui.event_observation_update import persist_current_pt_observation
from module.webui.event_source import (
    _current_pt_evidence_is_newer,
    empty_event_user_state,
    event_plan_from_source,
)


def test_observation_round_trip_is_event_server_and_profile_scoped(tmp_path: Path):
    root = tmp_path / "observations"
    revision = "a" * 40
    observation = empty_event_observation("en:5941", "EN", "alpha", revision)
    observation.update(
        {
            "observed_at": "2026-08-13T10:00:00+00:00",
            "source": "dashboard_ocr",
            "current_pt": 1234,
        }
    )
    save_event_observation("alpha", observation, root=root)

    assert (
        load_event_observation("alpha", "en:5941", "EN", revision, root=root)[
            "current_pt"
        ]
        == 1234
    )
    assert (
        load_event_observation("beta", "en:5941", "EN", root=root)["current_pt"] is None
    )
    assert (
        load_event_observation("alpha", "en:other", "EN", root=root)["current_pt"]
        is None
    )
    assert (
        load_event_observation(
            "alpha", "en:5941", "EN", "b" * 40, root=root
        )["current_pt"]
        is None
    )
    assert event_observation_path(
        "alpha", "en:5941", "EN", root, source_revision=revision
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
    with pytest.raises(ValueError, match="fixture/replay"):
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
        load_builtin_artifact("rose_tower.json")["event_spec"],
        empty_event_user_state(),
        stale,
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


def test_event_shop_pt_ocr_is_persisted_under_exact_revision(tmp_path: Path):
    observed = datetime.now(timezone.utc)
    revision = "c" * 40

    result = persist_current_pt_observation(
        instance="ap",
        event_id="en:51101",
        server="EN",
        source_revision=revision,
        value=0,
        observed_at=observed,
        root=tmp_path,
    )

    assert result["current_pt"] == 0
    assert result["current_pt_status"] == "observed"
    assert result["current_pt_source"] == "event_shop_ocr"
    assert load_event_observation(
        "ap", "en:51101", "EN", revision, root=tmp_path
    )["current_pt"] == 0
    assert load_event_observation(
        "ap", "en:51101", "EN", "d" * 40, root=tmp_path
    )["current_pt"] is None


def test_old_event_shop_pt_ocr_is_persisted_as_stale(tmp_path: Path):
    observed = datetime.now(timezone.utc) - timedelta(hours=49)

    result = persist_current_pt_observation(
        instance="ap",
        event_id="en:51101",
        server="EN",
        source_revision="c" * 40,
        value=123,
        observed_at=observed,
        root=tmp_path,
    )

    assert result["current_pt"] == 123
    assert result["current_pt_status"] == "stale"


def test_older_dashboard_evidence_cannot_replace_fresh_event_shop_ocr():
    stored = {
        "current_pt_observed_at": "2026-08-13T17:02:29+00:00",
        "current_pt": 0,
    }
    dashboard = {
        "current_pt_observed_at": "2026-08-11T10:00:00+00:00",
        "current_pt": 42,
    }

    assert not _current_pt_evidence_is_newer(dashboard, stored)
    assert _current_pt_evidence_is_newer(stored, dashboard)


def test_matching_runtime_identity_with_older_evidence_has_distinct_finding(monkeypatch):
    artifact = load_builtin_artifact("rose_tower.json")
    spec = artifact["event_spec"]
    event_id = str(spec["id"])
    server = str(spec.get("server") or "EN")
    revision = str(spec.get("provenance", {}).get("revision") or "")
    stored = empty_event_observation(event_id, server, "ap", revision)
    stored.update(
        {
            "current_pt": 100,
            "current_pt_source": "event_shop_ocr",
            "current_pt_observed_at": "2026-08-13T17:00:00+00:00",
            "current_pt_status": "observed",
        }
    )
    runtime = {
        "event_id": event_id,
        "server": server,
        "source_revision": revision,
        "current_pt": 90,
        "current_pt_source": "dashboard_ocr",
        "current_pt_observed_at": "2026-08-13T16:00:00+00:00",
        "current_pt_status": "observed",
    }
    monkeypatch.setattr(event_source, "load_event_observation", lambda *args, **kwargs: dict(stored))
    monkeypatch.setattr(event_source, "load_event_user_state", lambda *args, **kwargs: empty_event_user_state())

    plan = event_source.load_event_plan_from_artifact("ap", artifact, runtime)
    codes = {item.get("code") for item in plan["observation"]["findings"]}

    assert "runtime_observation_not_newer" in codes
    assert "runtime_observation_identity_rejected" not in codes


def test_newer_runtime_pt_without_status_does_not_inherit_stored_status(monkeypatch):
    artifact = load_builtin_artifact("rose_tower.json")
    spec = artifact["event_spec"]
    event_id = str(spec["id"])
    server = str(spec.get("server") or "EN")
    revision = str(spec.get("provenance", {}).get("revision") or "")
    now = datetime.now(timezone.utc)
    stored = empty_event_observation(event_id, server, "ap", revision)
    stored.update(
        {
            "current_pt": 100,
            "current_pt_source": "event_shop_ocr",
            "current_pt_observed_at": (now - timedelta(hours=1)).isoformat(),
            "current_pt_status": "stale",
        }
    )
    runtime_observed_at = now.isoformat()
    runtime = {
        "event_id": event_id,
        "server": server,
        "source_revision": revision,
        "current_pt": 120,
        "current_pt_source": "dashboard_ocr",
        "current_pt_observed_at": runtime_observed_at,
    }
    monkeypatch.setattr(
        event_source,
        "load_event_observation",
        lambda *args, **kwargs: dict(stored),
    )
    monkeypatch.setattr(
        event_source,
        "load_event_user_state",
        lambda *args, **kwargs: empty_event_user_state(),
    )

    plan = event_source.load_event_plan_from_artifact("ap", artifact, runtime)

    assert plan["progress"]["current_pt"] == 120
    assert plan["progress"]["status"] == "observed"
    assert plan["progress"]["source"] == "dashboard_ocr"
    assert plan["progress"]["observed_at"] == runtime_observed_at
