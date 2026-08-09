from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

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


def _custom_ship_names_frame(
    state: str,
    *,
    y: int = _REFERENCE_ORIGIN[1],
) -> np.ndarray:
    crop = _fixture(f"custom_ship_names_{state}.png")
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    x = _REFERENCE_ORIGIN[0]
    height, width = crop.shape[:2]
    frame[y : y + height, x : x + width] = crop
    return frame


class CustomShipNamesDetectorTests(unittest.TestCase):
    def test_real_on_fixture_detects_on(self) -> None:
        self.assertIs(
            detect_custom_ship_names(_custom_ship_names_frame("on")),
            GameSettingState.ON,
        )

    def test_real_off_fixture_detects_off(self) -> None:
        self.assertIs(
            detect_custom_ship_names(_custom_ship_names_frame("off")),
            GameSettingState.OFF,
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

    def test_row_visible_with_untrusted_markers_is_unknown(self) -> None:
        frame = _custom_ship_names_frame("off")
        with patch(
            "module.game_settings.detector._template_score",
            side_effect=[
                (0.99, (57, 409)),
                (0.98, (57, 409)),
                (0.20, (0, 0)),
                (0.20, (0, 0)),
            ],
        ):
            self.assertIs(
                detect_custom_ship_names(frame),
                GameSettingState.UNKNOWN,
            )

    def test_state_resolution_is_mutually_exclusive(self) -> None:
        cases = (
            (0.24, 0.91, GameSettingState.ON),
            (0.91, 0.24, GameSettingState.OFF),
            (0.64, 0.21, GameSettingState.UNKNOWN),
            (0.21, 0.64, GameSettingState.UNKNOWN),
            (0.86, 0.86, GameSettingState.UNKNOWN),
            (0.75, 0.86, GameSettingState.UNKNOWN),
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

    def test_production_assets_and_fixtures_are_same_real_crops(self) -> None:
        for state in ("on", "off"):
            with self.subTest(state=state):
                fixture = _fixture(f"custom_ship_names_{state}.png")
                asset = imageio.imread(
                    ASSET_DIR
                    / f"TEMPLATE_GAME_SETTINGS_CUSTOM_SHIP_NAMES_{state.upper()}.png"
                )
                asset = asset[:, :, :3] if asset.ndim == 3 else asset

                np.testing.assert_array_equal(asset, fixture)
                self.assertEqual(asset.shape[:2], (42, 440))

    def test_real_state_pixels_support_vertical_offset_search(self) -> None:
        for state, expected in (
            ("on", GameSettingState.ON),
            ("off", GameSettingState.OFF),
        ):
            with self.subTest(state=state):
                self.assertIs(
                    detect_custom_ship_names(
                        _custom_ship_names_frame(state, y=260)
                    ),
                    expected,
                )

    def test_detector_rejects_non_native_geometry(self) -> None:
        with self.assertRaisesRegex(ValueError, "1280 x 720"):
            detect_custom_ship_names(np.zeros((360, 640, 3), dtype=np.uint8))


if __name__ == "__main__":
    unittest.main()
