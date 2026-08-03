from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dev_tools.stage8a_binary_log_audit import find_binary_payload_log_findings


class Stage8ABinaryLogAuditTests(unittest.TestCase):
    def _scan(self, source: str):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "module/device/sample.py"
            path.parent.mkdir(parents=True)
            path.write_text(source, encoding="utf-8")
            return find_binary_payload_log_findings(root)

    def test_direct_screenshot_bytes_are_rejected(self):
        findings = self._scan(
            "def run(screenshot_bytes):\n"
            "    logger.info(screenshot_bytes)\n"
        )
        self.assertEqual(len(findings), 1)
        self.assertIn("screenshot_bytes", findings[0]["references"])

    def test_binary_value_inside_fstring_is_rejected(self):
        findings = self._scan(
            "def run(frame):\n"
            "    logger.debug(f'frame={frame}')\n"
        )
        self.assertEqual(len(findings), 1)
        self.assertIn("frame", findings[0]["references"])

    def test_neutral_name_under_length_guard_is_rejected(self):
        findings = self._scan(
            "def run(data):\n"
            "    if len(data) < 500:\n"
            "        logger.warning(f'invalid={data}')\n"
        )
        self.assertEqual(len(findings), 1)
        self.assertIn("data", findings[0]["references"])

    def test_byte_count_and_image_metadata_are_allowed(self):
        findings = self._scan(
            "def run(payload, image):\n"
            "    logger.info(f'bytes={len(payload)}, shape={image.shape}, dtype={image.dtype}')\n"
        )
        self.assertEqual(findings, [])

    def test_metadata_variable_names_are_allowed(self):
        findings = self._scan(
            "def run(frame_count, payload_size, image_dtype):\n"
            "    logger.info(f'count={frame_count}, bytes={payload_size}, dtype={image_dtype}')\n"
        )
        self.assertEqual(findings, [])

    def test_camel_case_and_timer_metadata_are_allowed(self):
        findings = self._scan(
            "def run(config, stuck_image_timer, IMAGE_TRUNCATED_THRESHOLD):\n"
            "    logger.info(config.Emulator_ScreenshotMethod)\n"
            "    logger.info(stuck_image_timer.limit)\n"
            "    logger.info(IMAGE_TRUNCATED_THRESHOLD)\n"
        )
        self.assertEqual(findings, [])

    def test_hex_or_decode_does_not_make_binary_payload_safe(self):
        findings = self._scan(
            "def run(packet):\n"
            "    logger.warning(packet.hex())\n"
        )
        self.assertEqual(len(findings), 1)

    def test_unrelated_dynamic_text_is_not_classified_as_binary(self):
        findings = self._scan(
            "def run(message):\n"
            "    logger.error(message)\n"
        )
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
