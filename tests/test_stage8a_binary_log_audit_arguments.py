from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dev_tools.stage8a_binary_log_audit import find_binary_payload_log_findings


class Stage8ABinaryLogAuditArgumentsTests(unittest.TestCase):
    def _findings(self, source: str):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "module" / "device" / "fixture.py"
            target.parent.mkdir(parents=True)
            target.write_text(source, encoding="utf-8")
            return find_binary_payload_log_findings(root)

    def test_lazy_formatting_second_argument_is_checked(self):
        findings = self._findings("logger.info('Frame: %s', screenshot)\n")
        self.assertEqual(findings[0]["references"], ["screenshot"])

    def test_keyword_argument_is_checked(self):
        findings = self._findings("logger.error('Payload failed', payload=raw_data)\n")
        self.assertEqual(findings[0]["references"], ["raw_data"])

    def test_logger_attr_value_is_checked(self):
        findings = self._findings("logger.attr('Image', image)\n")
        self.assertEqual(findings[0]["references"], ["image"])

    def test_nested_container_is_checked(self):
        findings = self._findings("logger.info('State %s', {'frame': video_frame})\n")
        self.assertEqual(findings[0]["references"], ["video_frame"])

    def test_safe_metadata_in_all_arguments_passes(self):
        findings = self._findings(
            "logger.info('Frame bytes=%s shape=%s dtype=%s', "
            "len(payload), image.shape, image.dtype)\n"
        )
        self.assertEqual(findings, [])

    def test_safe_keyword_metadata_passes(self):
        findings = self._findings(
            "logger.attr('ImageShape', image.shape, backend=backend)\n"
        )
        self.assertEqual(findings, [])


    def test_adb_binary_executable_path_is_safe_technical_metadata(self):
        findings = self._findings("logger.attr('AdbBinary', self.adb_binary)\n")
        self.assertEqual(findings, [])

    def test_other_binary_values_remain_blocked(self):
        findings = self._findings("logger.info('Blob: %s', payload_binary)\n")
        self.assertEqual(findings[0]["references"], ["payload_binary"])


if __name__ == "__main__":
    unittest.main()
