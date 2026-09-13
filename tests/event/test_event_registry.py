from datetime import datetime
from pathlib import Path

import pytest

from module.event_datamine.artifact import build_artifact, write_artifact
from module.event_datamine.discovery import EventDiscoveryError
from module.event_datamine.registry import (
    EventArtifactRegistry,
    build_registry,
    load_event_artifact_registry,
    write_registry,
)


def _artifact(event_id, start, farm_end, shop_end, *, role="production"):
    return build_artifact(
        {
            "id": event_id,
            "server": "EN",
            "farm_start": start,
            "farm_end": farm_end,
            "shop_end": shop_end,
            "source_status": "verified",
            "provenance": {"revision": "a" * 40},
        },
        role=role,
    )


def _write(root: Path, name: str, artifact):
    write_artifact(root / name, artifact)
    write_registry(root)


def test_registry_excludes_demo_and_selects_active_then_redemption(tmp_path: Path):
    write_artifact(
        tmp_path / "demo.json",
        _artifact("en:demo", "2026-01-01", "2026-12-01", "2026-12-02", role="demo"),
    )
    write_artifact(
        tmp_path / "active.json",
        _artifact("en:active", "2026-08-01", "2026-08-20", "2026-08-27"),
    )
    write_registry(tmp_path)
    registry = EventArtifactRegistry(tmp_path)

    assert registry.resolve_current("EN", datetime(2026, 8, 10))["event_spec"]["id"] == "en:active"
    assert registry.resolve_current("EN", datetime(2026, 8, 25))["event_spec"]["id"] == "en:active"
    assert registry.resolve_current("EN", datetime(2027, 1, 1)) is None


def test_registry_campaign_selector_survives_expired_lifecycle(tmp_path: Path):
    artifact = _artifact(
        "en:expired",
        "2026-08-01",
        "2026-08-20",
        "2026-08-27",
    )
    write_artifact(tmp_path / "expired.json", artifact)
    write_registry(
        tmp_path,
        campaign_selector={
            "server": "EN",
            "selector": "event_20260801_cn",
            "event_id": "en:expired",
        },
    )
    registry = EventArtifactRegistry(tmp_path)

    assert registry.resolve_current("EN", datetime(2027, 1, 1)) is None
    resolved = registry.resolve_campaign_selector("en", "event_20260801_cn")
    assert resolved is not None
    assert resolved["event_spec"]["id"] == "en:expired"


def test_registry_campaign_selector_upsert_preserves_other_bindings(tmp_path: Path):
    write_artifact(
        tmp_path / "first.json",
        _artifact("en:first", "2026-08-01", "2026-08-20", "2026-08-27"),
    )
    write_artifact(
        tmp_path / "second.json",
        _artifact("en:second", "2026-09-01", "2026-09-20", "2026-09-27"),
    )
    write_registry(
        tmp_path,
        campaign_selector={
            "server": "EN",
            "selector": "event_first_cn",
            "event_id": "en:first",
        },
    )
    write_registry(
        tmp_path,
        campaign_selector={
            "server": "EN",
            "selector": "event_second_cn",
            "event_id": "en:second",
        },
    )
    registry = EventArtifactRegistry(tmp_path)

    assert registry.resolve_campaign_selector("EN", "event_first_cn")["event_spec"]["id"] == "en:first"
    assert registry.resolve_campaign_selector("EN", "event_second_cn")["event_spec"]["id"] == "en:second"

    write_registry(
        tmp_path,
        campaign_selector={
            "server": "EN",
            "selector": "event_first_cn",
            "event_id": "en:second",
        },
    )
    refreshed = EventArtifactRegistry(tmp_path)
    assert refreshed.resolve_campaign_selector("EN", "event_first_cn")["event_spec"]["id"] == "en:second"
    assert refreshed.resolve_campaign_selector("EN", "event_second_cn")["event_spec"]["id"] == "en:second"


def test_registry_rejects_selector_to_missing_artifact(tmp_path: Path):
    write_artifact(
        tmp_path / "event.json",
        _artifact("en:event", "2026-08-01", "2026-08-20", "2026-08-27"),
    )

    with pytest.raises(ValueError, match="не разрешается в один Event artifact"):
        write_registry(
            tmp_path,
            campaign_selector={
                "server": "EN",
                "selector": "event_missing_cn",
                "event_id": "en:missing",
            },
        )


def test_registry_multiple_active_events_fail_closed(tmp_path: Path):
    for suffix in ("one", "two"):
        write_artifact(
            tmp_path / f"{suffix}.json",
            _artifact(f"en:{suffix}", "2026-08-01", "2026-08-20", "2026-08-27"),
        )
    write_registry(tmp_path)
    with pytest.raises(EventDiscoveryError) as caught:
        EventArtifactRegistry(tmp_path).resolve_current("EN", datetime(2026, 8, 10))
    assert caught.value.code == "ambiguous_active_event"


def test_registry_detects_corrupt_artifact_and_duplicate_identity(tmp_path: Path):
    artifact = _artifact("en:same", "2026-08-01", "2026-08-20", "2026-08-27")
    write_artifact(tmp_path / "one.json", artifact)
    write_artifact(tmp_path / "two.json", artifact)
    write_registry(tmp_path)
    with pytest.raises(ValueError, match="duplicate event identity"):
        EventArtifactRegistry(tmp_path)

    (tmp_path / "two.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        EventArtifactRegistry(tmp_path)


def test_registry_digest_tampering_is_rejected(tmp_path: Path):
    _write(
        tmp_path,
        "event.json",
        _artifact("en:event", "2026-08-01", "2026-08-20", "2026-08-27"),
    )
    index = tmp_path / "index.json"
    index.write_text(index.read_text(encoding="utf-8").replace('"digest": "', '"digest": "bad'), encoding="utf-8")
    with pytest.raises(ValueError, match="Digest Event registry"):
        EventArtifactRegistry(tmp_path)


def test_registry_build_rejects_corrupt_artifact_instead_of_skipping_it(tmp_path: Path):
    (tmp_path / "corrupt.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="Некорректный Event artifact"):
        build_registry(tmp_path)


def test_registry_cache_tracks_exact_index_revision(tmp_path: Path):
    _write(
        tmp_path,
        "event.json",
        _artifact("en:first", "2026-08-01", "2026-08-20", "2026-08-27"),
    )

    first = load_event_artifact_registry(tmp_path)
    repeated = load_event_artifact_registry(tmp_path)

    assert repeated is first
    assert first.resolve_current("EN", datetime(2026, 8, 10))["event_spec"]["id"] == "en:first"

    _write(
        tmp_path,
        "event.json",
        _artifact("en:second", "2026-08-01", "2026-08-20", "2026-08-27"),
    )
    refreshed = load_event_artifact_registry(tmp_path)

    assert refreshed is not first
    assert refreshed.resolve_current("EN", datetime(2026, 8, 10))["event_spec"]["id"] == "en:second"
