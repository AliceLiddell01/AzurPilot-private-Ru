from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import numpy as np

from module.dock_inventory.model import CanonicalShipIdentity, IdentityStatus, ShipForm
from module.dorm.morale_controller import DormMoraleController
from module.dorm.morale_model import (
    DormFloor,
    DormFloorScanAttempt,
    DormFloorScanStatus,
    DormFloorSnapshot,
    DormMoraleObservation,
    DormMoraleScanResult,
)


def _floor_1_frame():
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    frame[90:120, 145:330] = 255
    return frame


class _Device:
    def __init__(self, frame):
        self.image = frame
        self.clicks = []

    def screenshot(self):
        return None

    def click(self, button):
        self.clicks.append(button)


class _Controller(DormMoraleController):
    def _open_train(self):
        return self.device.image

    def _select_floor(self, frame, floor):
        assert floor is DormFloor.FLOOR_1
        return frame

    def appear(self, button, offset=(0, 0), interval=0):
        del button, offset, interval
        self._appear_calls += 1
        return self._appear_calls >= 2


def _scan():
    observation = DormMoraleObservation(
        floor=DormFloor.FLOOR_1,
        ordinal=1,
        raw_name_ocr="Argus",
        displayed_name="Argus",
        identity_status=IdentityStatus.MATCHED,
        morale=Decimal(150),
        recovery_per_hour=Decimal(40),
        canonical_identity=CanonicalShipIdentity("azur_lane_ship_group:1"),
        canonical_name="Argus",
        ship_form=ShipForm.BASE,
    )
    attempts = (
        DormFloorScanAttempt(
            floor=DormFloor.FLOOR_1,
            status=DormFloorScanStatus.SUCCEEDED,
            observed_at=datetime(2026, 8, 29, tzinfo=UTC),
            snapshot=DormFloorSnapshot(DormFloor.FLOOR_1, (observation,), "a" * 64),
        ),
        DormFloorScanAttempt(
            floor=DormFloor.FLOOR_2,
            status=DormFloorScanStatus.SUCCEEDED,
            observed_at=datetime(2026, 8, 29, tzinfo=UTC),
            snapshot=DormFloorSnapshot(DormFloor.FLOOR_2, (), "a" * 64),
        ),
    )
    return DormMoraleScanResult(
        id=uuid4(),
        started_at=datetime(2026, 8, 29, tzinfo=UTC),
        finished_at=datetime(2026, 8, 29, tzinfo=UTC),
        attempts=attempts,
        source="test:candidate",
        idempotency_key=f"scan:{uuid4()}",
    )


def test_first_train_ordinal_clicks_first_existing_occupant_and_never_confirm():
    controller = object.__new__(_Controller)
    controller.device = _Device(_floor_1_frame())
    controller._appear_calls = 0

    controller.open_candidate_selection(_scan())

    assert len(controller.device.clicks) == 1
    clicked = controller.device.clicks[0]
    assert clicked.name == "DORM_MORALE_EXISTING_TRAIN_OCCUPANT"
    assert clicked.button == controller.dorm_train_layout.train_card_buttons[0]
    assert "CONFIRM" not in clicked.name
