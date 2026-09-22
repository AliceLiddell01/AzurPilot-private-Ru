from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from module.application.errors import OperationFailedError
from module.application.legacy_game_adapters import LegacyGameApplicationAdapter
from module.application.live_resource_ocr import read_main_oil_snapshot
from tests.support.paths import FIXTURES_ROOT

FIXTURE = FIXTURES_ROOT / "game" / "resources" / "en_main_oil_25000_max_17050.png"


@pytest.fixture(scope="module")
def main_frame() -> np.ndarray:
    image = cv2.imread(str(FIXTURE), cv2.IMREAD_COLOR)
    assert image is not None
    return image


def test_main_global_fixture_reads_oil_and_displayed_limit(
    main_frame: np.ndarray,
) -> None:
    value, limit = read_main_oil_snapshot(main_frame)

    assert (value, limit) == (25000, 17050)


def test_oil_above_displayed_limit_is_a_valid_observation(
    main_frame: np.ndarray,
) -> None:
    value, limit = read_main_oil_snapshot(main_frame)

    assert value > limit


def test_masked_main_anchor_fails_closed_instead_of_returning_zero(
    main_frame: np.ndarray,
) -> None:
    corrupted = main_frame.copy()
    corrupted[0:20, 525:615] = 0

    with pytest.raises(OperationFailedError, match="anchor"):
        read_main_oil_snapshot(corrupted)


def test_legacy_adapter_uses_one_fresh_frame_and_explicit_authority(
    main_frame: np.ndarray,
) -> None:
    class Device:
        image = main_frame

        def __init__(self) -> None:
            self.screenshot_calls = 0
            self.release_calls = 0

        def screenshot(self) -> np.ndarray:
            self.screenshot_calls += 1
            return self.image

        def release_resource(self) -> None:
            self.release_calls += 1

    device = Device()
    adapter = LegacyGameApplicationAdapter(
        config_factory=lambda instance: object(),
        device_factory=lambda config: device,
    )

    observation = adapter.read_live_resources("ap")

    assert device.screenshot_calls == 1
    assert device.release_calls == 1
    assert observation.current_state_authority is True
    assert observation.source == "main_home_resource_bar_ocr"
    assert observation.resources.items[0].value == 25000
    assert observation.resources.items[0].limit == 17050


def test_fixture_is_a_real_sanitized_png() -> None:
    assert FIXTURE.is_file()
    assert FIXTURE.suffix == ".png"
    assert Path(FIXTURE).stat().st_size > 10_000
