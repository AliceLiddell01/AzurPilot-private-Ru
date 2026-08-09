from __future__ import annotations

import unittest
from pathlib import Path

import imageio.v2 as imageio
import numpy as np

from module.game_settings.detector import (
    _resolve_custom_ship_names_state,
    detect_custom_ship_names,
)
from module.game_settings.model import GameSettingState


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "game_settings"
ASSET_DIR = ROOT / "assets" / "en" / "game_settings"
_REFERENCE_ORIGIN = (226, 490)


def _fixture(name: str) -> np.ndarray:
    image = imageio.imread(FIXTURE_DIR / name)
    return image[:, :, :3] if image.ndim == 3 else image


def _custom_ship_names_frame() -> np.ndarray:
    crop = _fixture("custom_ship_names_on.png")
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    x, y = _REFERENCE_ORIGIN
    height, width = crop.shape[:2]
    frame[y : y + height, x : x + width] = crop
    return frame


class CustomShipNamesDetectorTests(unittest.TestCase):
    def test_real_on_fixture_detects_on(self) -> None:
        self.assertIs(
            detect_custom_ship_names(_custom_ship_names_frame()),
            GameSettingState.ON,
        )

    def test_existing_stage4_viewports_do_not_false_positive(self) -> None:
        for name in (
            "options_traversal_top.png",
            "options_traversal_middle_previous.png",
            "options_traversal_middle.png",
            "options_traversal_bottom.png",
            "options_traversal_bottom_retry.png",
        ):
            with self.subTest(fixture=name):
                self.assertIsNone(detect_custom_ship_names(_fixture(name)))

    def test_missing_row_is_not_reported_as_unknown_state(self) -> None:
        blank = np.zeros((720, 1280, 3), dtype=np.uint8)

        self.assertIsNone(detect_custom_ship_names(blank))

    def test_state_resolution_is_tristate_and_margin_guarded(self) -> None:
        cases = (
            (0.91, 0.24, GameSettingState.OFF),
            (0.24, 0.91, GameSettingState.ON),
            (0.64, 0.21, GameSettingState.UNKNOWN),
            (0.86, 0.75, GameSettingState.UNKNOWN),
        )

        for off_similarity, on_similarity, expected in cases:
            with self.subTest(
                off_similarity=off_similarity,
                on_similarity=on_similarity,
            ):
                self.assertIs(
                    _resolve_custom_ship_names_state(
                        off_similarity,
                        on_similarity,
                    ),
                    expected,
                )

    def test_production_asset_and_fixture_are_same_exact_real_crop(self) -> None:
        fixture = _fixture("custom_ship_names_on.png")
        asset = imageio.imread(
            ASSET_DIR / "TEMPLATE_GAME_SETTINGS_CUSTOM_SHIP_NAMES_ROW.png"
        )
        asset = asset[:, :, :3] if asset.ndim == 3 else asset

        np.testing.assert_array_equal(asset, fixture)
        self.assertEqual(asset.shape[:2], (42, 440))

    def test_detector_rejects_non_native_geometry(self) -> None:
        with self.assertRaisesRegex(ValueError, "1280 x 720"):
            detect_custom_ship_names(np.zeros((360, 640, 3), dtype=np.uint8))


if __name__ == "__main__":
    unittest.main()
