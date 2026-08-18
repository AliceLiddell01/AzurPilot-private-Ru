from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path

from tools.ci_heartbeat import run_command


class CiHeartbeatTestCase(unittest.TestCase):
    def test_success_forwards_output_and_writes_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "command.log"
            output = io.StringIO()

            exit_code = run_command(
                [sys.executable, "-c", "print('готово')"],
                label="Тестовая команда",
                heartbeat_seconds=0,
                log_file=log_file,
                output=output,
            )

            self.assertEqual(exit_code, 0)
            self.assertIn("готово", output.getvalue())
            self.assertIn("готово", log_file.read_text(encoding="utf-8"))
            self.assertIn("код выхода 0", log_file.read_text(encoding="utf-8"))

    def test_nonzero_exit_code_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "command.log"

            exit_code = run_command(
                [sys.executable, "-c", "raise SystemExit(7)"],
                label="Неуспешная команда",
                heartbeat_seconds=0,
                log_file=log_file,
                output=io.StringIO(),
            )

            self.assertEqual(exit_code, 7)
            self.assertIn("код выхода 7", log_file.read_text(encoding="utf-8"))

    def test_heartbeat_reports_silent_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "command.log"
            output = io.StringIO()

            exit_code = run_command(
                [sys.executable, "-c", "import time; time.sleep(0.2)"],
                label="Тихая команда",
                heartbeat_seconds=0.05,
                log_file=log_file,
                output=output,
            )

            self.assertEqual(exit_code, 0)
            self.assertIn("[heartbeat] Тихая команда", output.getvalue())
            self.assertIn("[heartbeat] Тихая команда", log_file.read_text(encoding="utf-8"))

    def test_negative_heartbeat_interval_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "не может быть отрицательным"):
                run_command(
                    [sys.executable, "-c", "pass"],
                    label="Некорректная команда",
                    heartbeat_seconds=-1,
                    log_file=Path(temp_dir) / "command.log",
                    output=io.StringIO(),
                )


if __name__ == "__main__":
    unittest.main()
