import os

import cv2
import pytest

from module.dock_inventory.model import IdentityStatus, ShipForm
from module.dorm.morale_controller import DormManageStateDetector
from module.dorm.morale_model import DormFloor
from module.dorm.morale_scanner import DormMoraleScanner

EXPECTED = {
    DormFloor.FLOOR_1: (
        ("Langley II", "azur_lane_ship_group:10727", None, "150", "40"),
        ("Arizona", "azur_lane_ship_group:10504", None, "150", "40"),
        ("Charybdis", "azur_lane_ship_group:20230", None, "150", "40"),
        ("Hermione", "azur_lane_ship_group:20227", None, "150", "40"),
        ("Nubian", "azur_lane_ship_group:20137", None, "150", "40"),
    ),
    DormFloor.FLOOR_2: (
        ("Vanguard", "azur_lane_ship_group:20513", None, "150", "50"),
        ("Alabama", "azur_lane_ship_group:10520", None, "150", "50"),
        (
            "Essex (Retrofit)",
            "azur_lane_ship_group:10709",
            ShipForm.RETROFIT,
            "150",
            "50",
        ),
    ),
}


@pytest.mark.parametrize(
    ("environment", "floor"),
    [
        ("AZURPILOT_DORM_MORALE_FLOOR1_SCREENSHOT", DormFloor.FLOOR_1),
        ("AZURPILOT_DORM_MORALE_FLOOR2_SCREENSHOT", DormFloor.FLOOR_2),
    ],
)
def test_real_dorm_screenshot_is_stable(environment, floor):
    path = os.getenv(environment)
    if not path:
        pytest.skip(f"{environment} не задан: локальный acceptance test отключён")
    frame = cv2.imread(path)
    assert frame is not None

    detector = DormManageStateDetector()
    assert detector.selected_floor(frame) is floor

    scanner = DormMoraleScanner()
    actual_runs = []
    for _ in range(2):
        result = scanner.scan(frame, floor=floor)
        actual_runs.append(
            tuple(
                (
                    item.displayed_name,
                    item.canonical_identity.key if item.canonical_identity else None,
                    item.ship_form,
                    str(item.morale),
                    str(item.recovery_per_hour),
                )
                for item in result.observations
            )
        )
        assert all(item.raw_name_ocr for item in result.observations)
        assert all(
            item.identity_status is IdentityStatus.MATCHED
            for item in result.observations
        )
        assert all(item.floor is floor for item in result.observations)
    assert actual_runs == [EXPECTED[floor], EXPECTED[floor]]
