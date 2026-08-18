import pytest

from module.event_datamine.supplemental import (
    EventSupplementalError,
    supplemental_digest,
    validate_supplemental,
)


def _supplemental(maps):
    data = {
        "supplemental_schema_version": 1,
        "event_id": "en:1",
        "base_contract": {
            "activity_id": 1,
            "event_name": "Test Event",
            "map_count": len(maps),
            "milestone_count": 0,
            "server": "EN",
            "shop_count": 0,
            "source_revision": "a" * 40,
        },
        "task_classification": [],
        "shop_overrides": [],
        "resource_display_assets": [],
        "farm": {"maps": maps},
        "verification": {"shop": {}, "milestones": {}},
    }
    data["digest"] = supplemental_digest(data)
    return data


def test_supplemental_accepts_cross_map_unlock_reference_after_assembly():
    data = _supplemental(
        [
            {
                "map_id": 1,
                "chapter_name": "A1",
                "grants_event_pt": True,
                "base_points": 30,
                "unlock_requires": [],
            },
            {
                "map_id": 2,
                "chapter_name": "A2",
                "grants_event_pt": True,
                "base_points": 50,
                "unlock_requires": ["A1"],
                "daily_first_clear_multiplier": 3,
                "daily_limit": 1,
                "oil": {"per_run": 25},
            },
        ]
    )

    validated = validate_supplemental(data)

    assert validated["farm"]["maps"][1]["unlock_requires"] == ["A1"]


def test_supplemental_rejects_unknown_unlock_reference():
    data = _supplemental(
        [
            {
                "map_id": 1,
                "chapter_name": "A1",
                "grants_event_pt": True,
                "base_points": 30,
                "unlock_requires": ["MISSING"],
            }
        ]
    )

    with pytest.raises(EventSupplementalError, match="неизвестные карты"):
        validate_supplemental(data)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("daily_first_clear_multiplier", "3"),
        ("daily_first_clear_multiplier", 0),
        ("daily_limit", True),
        ("daily_limit", 0),
    ],
)
def test_supplemental_rejects_nonpositive_or_coerced_runtime_numbers(field, value):
    row = {
        "map_id": 1,
        "chapter_name": "A1",
        "grants_event_pt": True,
        "base_points": 30,
        "unlock_requires": [],
        field: value,
    }
    data = _supplemental([row])

    with pytest.raises(EventSupplementalError, match="положительным JSON integer"):
        validate_supplemental(data)


def test_supplemental_rejects_invalid_oil_per_run_before_projection():
    data = _supplemental(
        [
            {
                "map_id": 1,
                "chapter_name": "A1",
                "grants_event_pt": True,
                "base_points": 30,
                "unlock_requires": [],
                "oil": {"per_run": "25"},
            }
        ]
    )

    with pytest.raises(EventSupplementalError, match="oil.per_run"):
        validate_supplemental(data)


def test_supplemental_rejects_duplicate_chapter_names():
    data = _supplemental(
        [
            {
                "map_id": 1,
                "chapter_name": "A1",
                "grants_event_pt": True,
                "base_points": 30,
                "unlock_requires": [],
            },
            {
                "map_id": 2,
                "chapter_name": "A1",
                "grants_event_pt": True,
                "base_points": 40,
                "unlock_requires": [],
            },
        ]
    )

    with pytest.raises(EventSupplementalError, match="chapter_name содержит дубликаты"):
        validate_supplemental(data)
