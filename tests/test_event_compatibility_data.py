import json
from pathlib import Path

import pytest

from module.event_datamine.patches import (
    CompatibilityDataError,
    compatibility_digest,
    load_compatibility_data,
)


def test_rose_tower_structural_exceptions_are_loaded_from_verified_data():
    patches = load_compatibility_data("en:5941")

    assert len(patches) == 6
    assert {item.map_id for item in patches} == {
        1920004,
        1920005,
        1920006,
        1920024,
        1920025,
        1920026,
    }
    assert all(item.ignored_land_rotations == (10,) for item in patches)
    assert all(item.repository == "wess09/AzurPilot" for item in patches)
    assert all(len(item.revision) == 40 for item in patches)


def test_unknown_event_has_no_implicit_structural_exceptions(tmp_path: Path):
    assert load_compatibility_data("en:999999", root=tmp_path) == ()


def test_compatibility_data_rejects_tampered_digest(tmp_path: Path):
    root = tmp_path / "compatibility"
    root.mkdir()
    data = {
        "compatibility_schema_version": 1,
        "event_id": "en:1",
        "evidence": {
            "repository": "example/repository",
            "revision": "1" * 40,
        },
        "patches": [
            {
                "id": "test-land-code",
                "map_id": 10,
                "ignored_land_rotations": [10],
                "reason": "Проверяемое структурное исключение.",
                "source_path": "campaign/event/example.py",
            }
        ],
    }
    data["digest"] = compatibility_digest(data)
    data["patches"][0]["map_id"] = 11
    (root / "en-1.json").write_text(
        json.dumps(data, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(CompatibilityDataError, match="Digest"):
        load_compatibility_data("en:1", root=root)
