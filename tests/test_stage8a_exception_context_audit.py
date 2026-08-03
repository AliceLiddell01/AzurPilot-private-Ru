from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dev_tools.stage8a_exception_context_audit import (
    find_bare_exception_context_findings,
)


class Stage8AExceptionContextAuditTests(unittest.TestCase):
    def _findings(self, source: str):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "module" / "device" / "fixture.py"
            target.parent.mkdir(parents=True)
            target.write_text(source, encoding="utf-8")
            return find_bare_exception_context_findings(root)

    def test_bare_logger_error_is_blocked(self):
        findings = self._findings(
            "try:\n    work()\nexcept RuntimeError as e:\n    logger.error(e)\n"
        )
        self.assertEqual([item["kind"] for item in findings], ["bare_external_exception"])

    def test_bare_logger_exception_is_blocked(self):
        findings = self._findings(
            "try:\n    work()\nexcept Exception as error:\n    logger.exception(error)\n"
        )
        self.assertEqual([item["kind"] for item in findings], ["bare_external_exception"])

    def test_str_exception_without_context_is_blocked(self):
        findings = self._findings(
            "try:\n    work()\nexcept Exception as exc:\n    logger.warning(str(exc))\n"
        )
        self.assertEqual([item["kind"] for item in findings], ["bare_external_exception"])

    def test_russian_context_and_raw_exception_pass(self):
        findings = self._findings(
            "try:\n    work()\nexcept RuntimeError as e:\n"
            "    logger.error(f'[Устройство — ADB] Не удалось переподключиться: {e}')\n"
        )
        self.assertEqual(findings, [])

    def test_lazy_formatting_with_russian_context_passes(self):
        findings = self._findings(
            "try:\n    work()\nexcept RuntimeError as e:\n"
            "    logger.error('[Устройство — ADB] Ошибка: %s', e)\n"
        )
        self.assertEqual(findings, [])

    def test_logger_exception_keeps_traceback_with_context(self):
        findings = self._findings(
            "try:\n    work()\nexcept Exception as e:\n"
            "    logger.exception(f'[Устройство — MaaTouch] Неизвестная ошибка: {e}')\n"
        )
        self.assertEqual(findings, [])

    def test_non_exception_raw_external_payload_is_not_misclassified(self):
        findings = self._findings(
            "try:\n    work()\nexcept Exception as e:\n"
            "    logger.info(ret)\n"
            "    logger.error(f'[Устройство] Ошибка: {e}')\n"
        )
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
