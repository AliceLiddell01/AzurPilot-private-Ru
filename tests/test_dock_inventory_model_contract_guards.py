import pytest

from module.dock_inventory.model import (
    CanonicalShipIdentity,
    DockInventoryScanResult,
    DockShipObservation,
    StarObservation,
)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("identity_status", "matched"),
        ("affinity", "oath"),
    ],
)
def test_observation_rejects_arbitrary_string_statuses(
    field_name: str,
    value: str,
) -> None:
    with pytest.raises(TypeError):
        DockShipObservation(ordinal=0, **{field_name: value})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("filled", 0.5),
        ("empty", 0.5),
        ("total", 1.0),
        ("filled", True),
        ("empty", False),
        ("total", True),
    ],
)
def test_star_observation_rejects_non_integer_counts(
    field_name: str,
    value: object,
) -> None:
    values: dict[str, object] = {"filled": 0, "empty": 0, "total": 0}
    values[field_name] = value

    with pytest.raises(TypeError, match=field_name):
        StarObservation(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("ordinal", [0.5, True, False])
def test_observation_rejects_non_integer_ordinal(ordinal: object) -> None:
    with pytest.raises(TypeError, match="ordinal"):
        DockShipObservation(ordinal=ordinal)  # type: ignore[arg-type]


@pytest.mark.parametrize("level", [1.5, True, False])
def test_observation_rejects_non_integer_level(level: object) -> None:
    with pytest.raises(TypeError, match="level"):
        DockShipObservation(ordinal=0, level=level)  # type: ignore[arg-type]


def test_canonical_identity_rejects_non_string_key() -> None:
    with pytest.raises(TypeError, match="string"):
        CanonicalShipIdentity(key=1)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("raw_name_ocr", []),
        ("displayed_name", {}),
        ("canonical_name", 1),
    ],
)
def test_observation_rejects_non_string_name_fields(
    field_name: str,
    value: object,
) -> None:
    with pytest.raises(TypeError, match=field_name):
        DockShipObservation(ordinal=0, **{field_name: value})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field_name", "value", "expected_message"),
    [
        ("canonical_identity", object(), "CanonicalShipIdentity"),
        ("stars", object(), "StarObservation"),
    ],
)
def test_observation_rejects_invalid_nested_model_types(
    field_name: str,
    value: object,
    expected_message: str,
) -> None:
    with pytest.raises(TypeError, match=expected_message):
        DockShipObservation(ordinal=0, **{field_name: value})  # type: ignore[arg-type]


def test_scan_result_rejects_mutable_observation_container() -> None:
    with pytest.raises(TypeError, match="tuple"):
        DockInventoryScanResult(  # type: ignore[arg-type]
            observations=[DockShipObservation(ordinal=0)]
        )


def test_scan_result_rejects_non_observation_values() -> None:
    with pytest.raises(TypeError, match="DockShipObservation"):
        DockInventoryScanResult(observations=(object(),))  # type: ignore[arg-type]
