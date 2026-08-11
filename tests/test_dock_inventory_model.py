from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from module.dock_inventory.model import (
    AffinityState,
    CanonicalShipIdentity,
    DockInventoryScanResult,
    DockShipObservation,
    IdentityStatus,
    StarObservation,
)


def _matched_observation(ordinal: int) -> DockShipObservation:
    return DockShipObservation(
        ordinal=ordinal,
        identity_status=IdentityStatus.MATCHED,
        raw_name_ocr="Enterprise",
        displayed_name="Enterprise",
        canonical_identity=CanonicalShipIdentity("fixture:enterprise"),
        canonical_name="Enterprise",
        level=125,
        stars=StarObservation(filled=6, empty=0, total=6),
        affinity=AffinityState.OATH,
    )


def test_identity_status_semantics_are_typed_and_minimal() -> None:
    assert {status.value for status in IdentityStatus} == {
        "unresolved",
        "matched",
        "ambiguous",
    }


def test_affinity_state_semantics_include_unknown() -> None:
    assert {state.value for state in AffinityState} == {
        "unknown",
        "below_100",
        "affinity_100",
        "oath",
    }


@pytest.mark.parametrize(
    ("filled", "empty", "total"),
    [
        (-1, 1, 0),
        (1, -1, 0),
        (0, 0, -1),
        (2, 1, 4),
    ],
)
def test_star_observation_rejects_invalid_counts(
    filled: int,
    empty: int,
    total: int,
) -> None:
    with pytest.raises(ValueError):
        StarObservation(filled=filled, empty=empty, total=total)


def test_star_observation_accepts_raw_counts() -> None:
    stars = StarObservation(filled=4, empty=2, total=6)

    assert stars.filled == 4
    assert stars.empty == 2
    assert stars.total == 6


def test_unresolved_and_ambiguous_observations_allow_unknown_fields() -> None:
    unresolved = DockShipObservation(ordinal=0)
    ambiguous = DockShipObservation(
        ordinal=1,
        identity_status=IdentityStatus.AMBIGUOUS,
        raw_name_ocr="Ent?rprise",
        displayed_name="Enterprise",
        level=None,
        stars=None,
        affinity=AffinityState.UNKNOWN,
    )

    assert unresolved.identity_status is IdentityStatus.UNRESOLVED
    assert unresolved.level is None
    assert ambiguous.identity_status is IdentityStatus.AMBIGUOUS
    assert ambiguous.canonical_identity is None


def test_matched_observation_requires_canonical_identity_and_name() -> None:
    with pytest.raises(ValueError, match="canonical identity"):
        DockShipObservation(
            ordinal=0,
            identity_status=IdentityStatus.MATCHED,
            canonical_name="Enterprise",
        )

    with pytest.raises(ValueError, match="canonical name"):
        DockShipObservation(
            ordinal=0,
            identity_status=IdentityStatus.MATCHED,
            canonical_identity=CanonicalShipIdentity("fixture:enterprise"),
            canonical_name=" ",
        )


@pytest.mark.parametrize("level", [0, -1])
def test_observation_rejects_invalid_known_level(level: int) -> None:
    with pytest.raises(ValueError, match="level"):
        DockShipObservation(ordinal=0, level=level)


def test_observation_rejects_negative_ordinal() -> None:
    with pytest.raises(ValueError, match="ordinal"):
        DockShipObservation(ordinal=-1)


def test_non_matched_observation_cannot_claim_canonical_identity() -> None:
    with pytest.raises(ValueError, match="only matched"):
        DockShipObservation(
            ordinal=0,
            identity_status=IdentityStatus.AMBIGUOUS,
            canonical_identity=CanonicalShipIdentity("fixture:enterprise"),
        )


def test_duplicate_canonical_ships_are_preserved_as_two_observations() -> None:
    first = _matched_observation(ordinal=0)
    second = _matched_observation(ordinal=1)

    result = DockInventoryScanResult(observations=(first, second))

    assert len(result) == 2
    assert tuple(result) == (first, second)
    assert first.canonical_identity == second.canonical_identity
    assert first.canonical_name == second.canonical_name
    assert first.level == second.level
    assert first.stars == second.stars
    assert first.affinity == second.affinity


def test_scan_result_preserves_insertion_order_not_ordinal_sorting() -> None:
    first = DockShipObservation(ordinal=2)
    second = DockShipObservation(ordinal=1)

    result = DockInventoryScanResult(observations=(first, second))

    assert tuple(result) == (first, second)


def test_scan_result_allows_deterministic_empty_result() -> None:
    result = DockInventoryScanResult()

    assert len(result) == 0
    assert tuple(result) == ()


def test_scan_result_rejects_duplicate_scan_local_ordinals() -> None:
    with pytest.raises(ValueError, match="unique"):
        DockInventoryScanResult(
            observations=(
                DockShipObservation(ordinal=0),
                DockShipObservation(ordinal=0),
            )
        )


def test_stage_1_model_objects_are_immutable() -> None:
    observation = _matched_observation(ordinal=0)
    stars = observation.stars
    result = DockInventoryScanResult(observations=(observation,))

    with pytest.raises(FrozenInstanceError):
        observation.level = 1  # type: ignore[misc]

    with pytest.raises(FrozenInstanceError):
        stars.filled = 1  # type: ignore[union-attr,misc]

    with pytest.raises(FrozenInstanceError):
        result.observations = ()  # type: ignore[misc]


def test_model_source_has_no_runtime_ui_cv_or_retire_dependencies() -> None:
    source = (
        Path(__file__).parents[1] / "module" / "dock_inventory" / "model.py"
    ).read_text(encoding="utf-8")

    forbidden_dependencies = (
        "cv2",
        "numpy",
        "module.device",
        "module.ui",
        "module.base.button",
        "module.retire",
        "module.game_settings",
        "sqlite3",
    )

    for dependency in forbidden_dependencies:
        assert dependency not in source
