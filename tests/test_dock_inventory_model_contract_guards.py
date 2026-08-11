import pytest

from module.dock_inventory.model import (
    DockInventoryScanResult,
    DockShipObservation,
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


def test_scan_result_rejects_mutable_observation_container() -> None:
    with pytest.raises(TypeError, match="tuple"):
        DockInventoryScanResult(  # type: ignore[arg-type]
            observations=[DockShipObservation(ordinal=0)]
        )
