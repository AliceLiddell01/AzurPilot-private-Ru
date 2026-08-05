from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import cv2
import numpy as np

from dev_tools.commission_ocr_acceptance import (
    _png_for_cv2 as commission_png_for_cv2,
)
from dev_tools.commission_ocr_acceptance import _write_png as write_commission_png
from dev_tools.stage8b_opsi_zone_acceptance import (
    _png_for_cv2 as opsi_png_for_cv2,
)
from dev_tools.stage8b_opsi_zone_acceptance import (
    _prepare_artifact_dir,
    _write_png as write_opsi_png,
    evaluate_samples,
)
from module.ocr.ocr import Ocr
from module.os.assets import MAP_NAME


class OperationSirenZoneAcceptanceTests(unittest.TestCase):
    def test_stable_zone_samples_pass(self) -> None:
        samples = [
            {
                "id": index,
                "raw_text": "Southeast Ocean Ridge B",
                "processed_name": "southeastoceanridgeb",
                "zone_id": 52,
            }
            for index in range(1, 6)
        ]

        self.assertEqual(evaluate_samples(samples), [])

    def test_old_compact_ocr_gibberish_fails_closed(self) -> None:
        samples = [
            {
                "id": index,
                "raw_text": "MA0656S6S6FSa162868",
                "processed_name": "ma0656s6s6fsa162868",
                "zone_id": None,
            }
            for index in range(1, 6)
        ]

        findings = evaluate_samples(samples)

        self.assertTrue(any("старый OCR-мусор" in item for item in findings))
        self.assertTrue(any("подозрительно много цифр" in item for item in findings))
        self.assertTrue(any("не сопоставлено с Zone" in item for item in findings))

    def test_unstable_zone_mapping_fails(self) -> None:
        samples = [
            {
                "id": 1,
                "raw_text": "Gibraltar",
                "processed_name": "gibraltar",
                "zone_id": 2,
            },
            {
                "id": 2,
                "raw_text": "Gibraltar",
                "processed_name": "gibraltar",
                "zone_id": 2,
            },
            {
                "id": 3,
                "raw_text": "Liverpool",
                "processed_name": "liverpool",
                "zone_id": 1,
            },
        ]

        findings = evaluate_samples(samples)

        self.assertTrue(any("разными зонами" in item for item in findings))

    def test_explicit_semantic_name_overrides_button_label(self) -> None:
        ocr = Ocr(MAP_NAME, name="OCR_OS_MAP_NAME")

        self.assertEqual(ocr.name, "OCR_OS_MAP_NAME")
        self.assertNotEqual(ocr.name, str(MAP_NAME))

    def test_actual_map_name_request_routes_to_general_english(self) -> None:
        ocr = Ocr(
            MAP_NAME,
            lang="azur_lane",
            letter=(206, 223, 247),
            threshold=96,
            name="OCR_OS_MAP_NAME",
        )

        selected = ocr.cnocr

        self.assertEqual(ocr.name, "OCR_OS_MAP_NAME")
        self.assertEqual(type(selected).__name__, "GeneralEnglishOcr")
        self.assertEqual(selected.name, "english_text")

    def test_acceptance_png_writers_preserve_rgb_colors(self) -> None:
        rgb = np.array(
            [[[255, 0, 0], [0, 255, 0], [0, 0, 255]]],
            dtype=np.uint8,
        )
        expected_bgr = np.array(
            [[[0, 0, 255], [0, 255, 0], [255, 0, 0]]],
            dtype=np.uint8,
        )

        for label, writer in (
            ("commission", write_commission_png),
            ("opsi", write_opsi_png),
        ):
            with self.subTest(writer=label), TemporaryDirectory() as temporary_directory:
                output = Path(temporary_directory) / f"{label}.png"
                writer(output, rgb)
                decoded = cv2.imread(str(output), cv2.IMREAD_COLOR)
                np.testing.assert_array_equal(decoded, expected_bgr)

    def test_acceptance_png_conversion_preserves_gray_and_alpha(self) -> None:
        gray = np.array([[0, 127, 255]], dtype=np.uint8)
        rgba = np.array([[[255, 0, 0, 128]]], dtype=np.uint8)
        expected_bgra = np.array([[[0, 0, 255, 128]]], dtype=np.uint8)

        for label, converter in (
            ("commission", commission_png_for_cv2),
            ("opsi", opsi_png_for_cv2),
        ):
            with self.subTest(converter=label):
                np.testing.assert_array_equal(converter(gray), gray)
                np.testing.assert_array_equal(converter(rgba), expected_bgra)

    def test_artifact_cleanup_removes_only_generated_images(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            artifact_dir = Path(temporary_directory)
            (artifact_dir / "sample-01-screen.png").write_bytes(b"stale")
            (artifact_dir / "sample-01-map-name.tmp").write_bytes(b"stale")
            keep = artifact_dir / "operator-note.txt"
            keep.write_text("keep", encoding="utf-8")

            removed = _prepare_artifact_dir(artifact_dir)

            self.assertEqual(
                removed,
                ["sample-01-screen.png", "sample-01-map-name.tmp"],
            )
            self.assertTrue(keep.is_file())

    def test_live_runner_is_bounded_and_read_only(self) -> None:
        source = Path("dev_tools/stage8b_opsi_zone_acceptance.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("runner.ui_ensure(page_os)", source)
        self.assertIn("runner.get_zone_name()", source)
        self.assertIn("runner.name_to_zone(processed_name)", source)
        self.assertIn("runner.name_to_zone(final_processed_name)", source)
        self.assertIn("MATCH ZONE", source)
        self.assertIn("cv2.COLOR_RGB2BGR", source)
        self.assertNotIn("runner.os_init(", source)
        self.assertNotIn("runner.zone_init(", source)
        self.assertNotIn("runner.run_auto_search(", source)
        self.assertNotIn("runner.get_current_zone(", source)
        self.assertNotIn("runner.get_current_zone_from_globe(", source)
        self.assertNotIn("runner.globe_update(", source)
        self.assertNotIn("runner.mission_checkout(", source)


if __name__ == "__main__":
    unittest.main()
