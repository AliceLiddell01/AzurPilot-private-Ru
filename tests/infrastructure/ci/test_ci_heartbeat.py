from __future__ import annotations

import io
import os
import signal
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from tools.ci_heartbeat import parse_args, run_command


class CiHeartbeatTestCase(unittest.TestCase):
    def test_success_forwards_output_and_writes_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "command.log"
            output = io.StringIO()

            exit_code = run_command(
                [sys.executable, "-X", "utf8", "-c", "print('готово')"],
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

    @unittest.skipIf(os.name == "nt", "Отрицательные returncode сигналов относятся к POSIX.")
    def test_signal_exit_code_is_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "command.log"

            exit_code = run_command(
                [
                    sys.executable,
                    "-c",
                    "import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
                ],
                label="Команда по сигналу",
                heartbeat_seconds=0,
                log_file=log_file,
                output=io.StringIO(),
            )

            expected_exit_code = 128 + signal.SIGTERM
            self.assertEqual(exit_code, expected_exit_code)
            self.assertIn(
                f"код выхода {expected_exit_code}",
                log_file.read_text(encoding="utf-8"),
            )

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

    def test_empty_command_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "Команда для запуска не задана"):
                run_command(
                    [],
                    label="Пустая команда",
                    heartbeat_seconds=0,
                    log_file=Path(temp_dir) / "command.log",
                    output=io.StringIO(),
                )

    def test_empty_label_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "Название CI-операции не задано"):
                run_command(
                    [sys.executable, "-c", "pass"],
                    label="   ",
                    heartbeat_seconds=0,
                    log_file=Path(temp_dir) / "command.log",
                    output=io.StringIO(),
                )

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

    def test_parse_args_removes_command_separator(self) -> None:
        args = parse_args(
            [
                "--label",
                "CLI",
                "--log-file",
                "runner.log",
                "--",
                sys.executable,
                "-V",
            ]
        )

        self.assertEqual(args.command, [sys.executable, "-V"])

    def test_parse_args_uses_default_heartbeat(self) -> None:
        args = parse_args(
            [
                "--label",
                "CLI",
                "--log-file",
                "runner.log",
                "--",
                sys.executable,
                "-V",
            ]
        )

        self.assertEqual(args.heartbeat_seconds, 60.0)

    def test_parse_args_rejects_missing_command(self) -> None:
        with redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(SystemExit, "2"):
                parse_args(
                    [
                        "--label",
                        "CLI",
                        "--log-file",
                        "runner.log",
                    ]
                )


if __name__ == "__main__":
    unittest.main()
