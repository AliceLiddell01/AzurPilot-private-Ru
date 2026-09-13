from __future__ import annotations

import unittest

import numpy as np

from module.game_settings.model import GameSettingsScanResult
from module.game_settings.scanner import GameSettingsScanner


class _ReusableBufferDevice:
    def __init__(self) -> None:
        self.backend_buffer = np.zeros((720, 1280, 3), dtype=np.uint8)
        self.image = self.backend_buffer

    def screenshot(self) -> np.ndarray:
        # Model a backend that reuses one mutable ndarray for every capture.
        self.image = self.backend_buffer
        return self.backend_buffer


class _StableFrameScanner(GameSettingsScanner):
    def __init__(self) -> None:
        self.device = _ReusableBufferDevice()

    def _scan_game_settings(self) -> GameSettingsScanResult:
        return GameSettingsScanResult()


class GameSettingsStableFrameContractTests(unittest.TestCase):
    def test_capture_exposes_detached_traversal_frame_to_visitor(self) -> None:
        scanner = _StableFrameScanner()
        scanner.device.backend_buffer[10, 20] = (11, 22, 33)

        frame = scanner._capture_options_frame()

        self.assertIs(frame, scanner.device.image)
        self.assertIsNot(frame, scanner.device.backend_buffer)
        np.testing.assert_array_equal(frame[10, 20], (11, 22, 33))

        scanner.device.backend_buffer[10, 20] = (99, 88, 77)
        np.testing.assert_array_equal(frame[10, 20], (11, 22, 33))


if __name__ == "__main__":
    unittest.main()
