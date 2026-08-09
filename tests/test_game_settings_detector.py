from __future__ import annotations

import hashlib
import unittest
from pathlib import Path
from unittest.mock import patch

import imageio.v2 as imageio
import numpy as np

from module.game_settings.detector import (
    _CUSTOM_SHIP_NAMES_LABEL_AREA,
    _CUSTOM_SHIP_NAMES_STATE_AREA,
    _resolve_custom_ship_names_state,
    detect_custom_ship_names,
)
from module.game_settings.model import GameSettingState


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "game_settings"
ASSET_DIR = ROOT / "assets" / "en" / "game_settings"
_REFERENCE_ORIGIN = (226, 490)
_OFF_STATE_SHA256 = "85346a4d0d255251ad6dab83a5d4f3fe04414885e6b6c947e8229671bbeb0a9f"


def _fixture(name: str) -> np.ndarray:
    image = imageio.imread(FIXTURE_DIR / name)
    return image[:, :, :3] if image.ndim == 3 else image


def _frame_from_real_state_evidence(
    state: str,
    *,
    y: int = _REFERENCE_ORIGIN[1],
) -> np.ndarray:
    """Build detector input from real screenshot crops without altering state pixels."""

    row = _fixture("custom_ship_names_on.png").copy()
    if state == "off":
        x1, y1, x2, y2 = _CUSTOM_SHIP_NAMES_STATE_AREA
        row[y1:y2, x1:x2] = _fixture("custom_ship_names_off_state.png")
    elif state != "on":
        raise ValueError(f"Unsupported state fixture: {state}")

    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    x = _REFERENCE_ORIGIN[0]
    height, width = row.shape[:2]
    frame[y : y + height, x : x + width] = row
    return frame


def _frame_with_independent_label_shift(
    state: str,
    *,
    dx: int = 0,
    dy: int = 0,
) -> np.ndarray:
    """Keep real state pixels fixed while moving only the unique text anchor."""

    row = _fixture("custom_ship_names_on.png")
    lx1, ly1, lx2, ly2 = _CUSTOM_SHIP_NAMES_LABEL_AREA
    sx1, sy1, sx2, sy2 = _CUSTOM_SHIP_NAMES_STATE_AREA
    label = row[ly1:ly2, lx1:lx2]

    if state == "on":
        state_pixels = row[sy1:sy2, sx1:sx2]
    elif state == "off":
        state_pixels = _fixture("custom_ship_names_off_state.png")
    else:
        raise ValueError(f"Unsupported state fixture: {state}")

    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    x, y = _REFERENCE_ORIGIN
    frame[y + sy1 : y + sy2, x + sx1 : x + sx2] = state_pixels

    label_x = x + lx1 + dx
    label_y = y + ly1 + dy
    label_height, label_width = label.shape[:2]
    frame[
        label_y : label_y + label_height,
        label_x : label_x + label_width,
    ] = label
    return frame


class CustomShipNamesDetectorTests(unittest.TestCase):
    def test_real_on_fixture_detects_on(self) -> None:
        self.assertIs(
            detect_custom_ship_names(_frame_from_real_state_evidence("on")),
            GameSettingState.ON,
        )

    def test_real_off_state_evidence_detects_off(self) -> None:
        self.assertIs(
            detect_custom_ship_names(_frame_from_real_state_evidence("off")),
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

    def test_row_visible_with_untrusted_state_is_unknown(self) -> None:
        frame = _frame_from_real_state_evidence("off")
        with patch(
            "module.game_settings.detector._template_score",
            side_effect=[
                (0.99, (57, 409)),
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

    def test_production_visual_assets_match_real_fixtures(self) -> None:
        on_fixture = _fixture("custom_ship_names_on.png")
        on_asset = imageio.imread(
            ASSET_DIR / "TEMPLATE_GAME_SETTINGS_CUSTOM_SHIP_NAMES_ON.png"
        )
        on_asset = on_asset[:, :, :3] if on_asset.ndim == 3 else on_asset
        np.testing.assert_array_equal(on_asset, on_fixture)
        self.assertEqual(on_asset.shape[:2], (42, 440))

        off_fixture_path = FIXTURE_DIR / "custom_ship_names_off_state.png"
        off_asset_path = (
            ASSET_DIR / "TEMPLATE_GAME_SETTINGS_CUSTOM_SHIP_NAMES_OFF_STATE.png"
        )
        off_fixture = _fixture(off_fixture_path.name)
        off_asset = imageio.imread(off_asset_path)
        off_asset = off_asset[:, :, :3] if off_asset.ndim == 3 else off_asset
        np.testing.assert_array_equal(off_asset, off_fixture)
        self.assertEqual(off_asset.shape[:2], (42, 165))
        self.assertEqual(
            hashlib.sha256(off_asset_path.read_bytes()).hexdigest(),
            _OFF_STATE_SHA256,
        )

    def test_real_state_pixels_support_vertical_offset_search(self) -> None:
        for state, expected in (
            ("on", GameSettingState.ON),
            ("off", GameSettingState.OFF),
        ):
            with self.subTest(state=state):
                self.assertIs(
                    detect_custom_ship_names(
                        _frame_from_real_state_evidence(state, y=260)
                    ),
                    expected,
                )

    def test_independent_label_shift_accepts_search_window_boundaries(self) -> None:
        boundary_offsets = (
            (-9, 0),
            (9, 0),
            (0, -13),
            (0, 5),
        )
        for state, expected in (
            ("on", GameSettingState.ON),
            ("off", GameSettingState.OFF),
        ):
            for dx, dy in boundary_offsets:
                with self.subTest(state=state, dx=dx, dy=dy):
                    self.assertIs(
                        detect_custom_ship_names(
                            _frame_with_independent_label_shift(
                                state,
                                dx=dx,
                                dy=dy,
                            )
                        ),
                        expected,
                    )

    def test_independent_label_shift_outside_search_window_is_unknown(self) -> None:
        outside_offsets = (
            (-10, 0),
            (10, 0),
            (0, -14),
            (0, 6),
        )
        for state in ("on", "off"):
            for dx, dy in outside_offsets:
                with self.subTest(state=state, dx=dx, dy=dy):
                    self.assertIs(
                        detect_custom_ship_names(
                            _frame_with_independent_label_shift(
                                state,
                                dx=dx,
                                dy=dy,
                            )
                        ),
                        GameSettingState.UNKNOWN,
                    )

    def test_detector_rejects_non_native_geometry(self) -> None:
        with self.assertRaisesRegex(ValueError, "1280 x 720"):
            detect_custom_ship_names(np.zeros((360, 640, 3), dtype=np.uint8))


if __name__ == "__main__":
    unittest.main()
