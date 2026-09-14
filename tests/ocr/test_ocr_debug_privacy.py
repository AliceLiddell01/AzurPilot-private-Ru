from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

from module.ocr.privacy import (
    OcrDebugOutputError,
    _is_reparse_point,
    cleanup_debug_directory,
    save_debug_image,
)


class OcrDebugPrivacyTests(unittest.TestCase):
    def test_debug_output_requires_opt_in_and_cleanup_removes_directory(self) -> None:
        image = np.zeros((8, 8, 3), dtype=np.uint8)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "debug"
            with patch.dict(os.environ, {"AZURPILOT_OCR_DEBUG": "0"}, clear=False):
                self.assertIsNone(
                    save_debug_image(image, model_name="azur_lane", directory=target)
                )
                self.assertFalse(target.exists())

            with patch.dict(os.environ, {"AZURPILOT_OCR_DEBUG": "1"}, clear=False):
                path = save_debug_image(
                    image,
                    model_name="azur_lane",
                    directory=target,
                )

            self.assertIsNotNone(path)
            assert path is not None
            self.assertTrue(path.is_file())
            self.assertNotIn("Operation Siren", path.name)
            self.assertFalse(
                any(entry.name.startswith(".ocr-") for entry in target.iterdir())
            )

            cleanup_debug_directory(target)
            self.assertFalse(target.exists())

    def test_retention_bounds_png_files_and_leaves_no_temporary_files(self) -> None:
        image = np.zeros((8, 8, 3), dtype=np.uint8)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "debug"
            with patch.dict(os.environ, {"AZURPILOT_OCR_DEBUG": "1"}, clear=False):
                for index in range(4):
                    save_debug_image(
                        image + index,
                        model_name="azur_lane",
                        directory=target,
                        retention=2,
                    )
                    time.sleep(0.002)

            png_files = list(target.glob("*.png"))
            self.assertEqual(len(png_files), 2)
            self.assertFalse(
                any(entry.name.startswith(".ocr-") for entry in target.iterdir())
            )

    def test_debug_output_rejects_repository_and_symlink_components(self) -> None:
        image = np.zeros((8, 8, 3), dtype=np.uint8)
        with patch.dict(os.environ, {"AZURPILOT_OCR_DEBUG": "1"}, clear=False):
            with self.assertRaises(OcrDebugOutputError):
                save_debug_image(
                    image,
                    model_name="azur_lane",
                    directory="ocr_debug",
                )

            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                real = root / "real"
                real.mkdir()
                link = root / "link"
                try:
                    link.symlink_to(real, target_is_directory=True)
                except (OSError, NotImplementedError):
                    self.skipTest("Symlink creation is unavailable")

                with self.assertRaises(OcrDebugOutputError):
                    save_debug_image(
                        image,
                        model_name="azur_lane",
                        directory=link / "debug",
                    )

    def test_junction_is_treated_as_reparse_point(self) -> None:
        path = MagicMock()
        path.is_symlink.return_value = False
        path.is_junction.return_value = True
        path.exists.return_value = True
        self.assertTrue(_is_reparse_point(path))

    def test_windows_file_attribute_reparse_point_is_rejected(self) -> None:
        path = MagicMock()
        path.is_symlink.return_value = False
        path.is_junction.return_value = False
        path.exists.return_value = True
        path.lstat.return_value.st_file_attributes = 0x400
        with patch("module.ocr.privacy.os.name", "nt"):
            self.assertTrue(_is_reparse_point(path))

    def test_cleanup_stops_before_following_reparse_component(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "debug"
            target.mkdir()
            with patch(
                "module.ocr.privacy._is_reparse_point",
                side_effect=lambda path: path == target,
            ):
                with self.assertRaises(OcrDebugOutputError):
                    cleanup_debug_directory(target)

    def test_atomic_publish_rechecks_target_path(self) -> None:
        image = np.zeros((8, 8, 3), dtype=np.uint8)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "debug"
            calls = {"count": 0}

            def detect(path: Path) -> bool:
                is_published_png = (
                    path.parent == target
                    and path.suffix == ".png"
                    and not path.name.startswith(".ocr-")
                )
                if is_published_png:
                    calls["count"] += 1
                    return calls["count"] >= 2
                return False

            with patch.dict(os.environ, {"AZURPILOT_OCR_DEBUG": "1"}, clear=False):
                with patch("module.ocr.privacy._is_reparse_point", side_effect=detect):
                    with self.assertRaises(OcrDebugOutputError):
                        save_debug_image(
                            image,
                            model_name="azur_lane",
                            directory=target,
                        )


if __name__ == "__main__":
    unittest.main()
