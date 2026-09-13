import json
from pathlib import Path

import pytest

from module.event_datamine.artifact import build_artifact, write_artifact
from module.event_datamine.registry import (
    EVENT_REGISTRY_SCHEMA_VERSION,
    build_registry,
    registry_digest,
    validate_registry,
    write_registry,
)


def _artifact(
    event_id: str,
    *,
    server: str = "EN",
    role: str = "production",
):
    return build_artifact(
        {
            "id": event_id,
            "server": server,
            "farm_start": "2026-08-01",
            "farm_end": "2026-08-20",
            "shop_end": "2026-08-27",
            "source_status": "verified",
            "provenance": {"revision": "a" * 40},
        },
        role=role,
    )


def _write(tmp_path: Path, artifact, name: str = "event.json") -> Path:
    target = tmp_path / "production" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    write_artifact(target, artifact)
    return target


def test_registry_rejects_non_mapping_artifact_entry_before_selector_resolution():
    data = {
        "registry_schema_version": EVENT_REGISTRY_SCHEMA_VERSION,
        "artifacts": ["broken"],
        "campaign_selectors": [],
    }
    data["digest"] = registry_digest(data)

    with pytest.raises(
        ValueError,
        match="некорректную запись artifacts",
    ):
        validate_registry(data)


def test_registry_schema_v1_fails_closed_with_regeneration_instruction(
    tmp_path: Path,
):
    target = tmp_path / "index.json"
    target.write_text(
        json.dumps(
            {
                "registry_schema_version": 1,
                "artifacts": [],
                "campaign_selectors": [],
                "digest": "legacy",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="Удалите только generated index.json",
    ):
        write_registry(tmp_path)


def test_registry_rejects_duplicate_selector_binding(tmp_path: Path):
    _write(tmp_path, _artifact("en:101"))
    binding = {
        "server": "EN",
        "selector": "event_fixture",
        "event_id": "en:101",
    }

    with pytest.raises(
        ValueError,
        match="дублирует campaign selector",
    ):
        build_registry(
            tmp_path,
            campaign_selectors=(binding, binding),
        )


def test_registry_rejects_selector_target_from_other_server(tmp_path: Path):
    _write(tmp_path, _artifact("jp:101", server="JP"))

    with pytest.raises(
        ValueError,
        match="другого сервера",
    ):
        build_registry(
            tmp_path,
            campaign_selectors=(
                {
                    "server": "EN",
                    "selector": "event_fixture",
                    "event_id": "jp:101",
                },
            ),
        )


def test_registry_rejects_selector_targeting_demo_artifact(tmp_path: Path):
    _write(
        tmp_path,
        _artifact("en:101", role="demo"),
        name="demo.json",
    )

    with pytest.raises(
        ValueError,
        match="только на production artifact",
    ):
        build_registry(
            tmp_path,
            campaign_selectors=(
                {
                    "server": "EN",
                    "selector": "event_fixture",
                    "event_id": "en:101",
                },
            ),
        )
