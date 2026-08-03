from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dev_tools.stage8a_device_acceptance import (
    AcceptanceFailure,
    _check_control,
    _resolve_serial,
    _run_adb,
    _safe_text,
    main,
)


class Stage8ADeviceAcceptanceTests(unittest.TestCase):
    def test_serial_from_config_must_be_explicit_not_auto(self):
        args = argparse.Namespace(serial=None, serial_from_config=True)
        with self.assertRaises(AcceptanceFailure):
            _resolve_serial(args, {"serial": "auto"})

    def test_serial_argument_overrides_environment_only_by_explicit_s_flag(self):
        args = argparse.Namespace(serial="emulator-5554", serial_from_config=False)
        self.assertEqual(
            _resolve_serial(args, {"serial": "emulator-5554"}),
            "emulator-5554",
        )

    @mock.patch("dev_tools.stage8a_device_acceptance.subprocess.run")
    def test_adb_command_is_target_explicit(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        _run_adb("adb", "emulator-5554", "get-state")
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["adb", "-s", "emulator-5554"])

    def test_noninteractive_control_never_sends_input(self):
        result = _check_control("adb", "emulator-5554", non_interactive=True)
        self.assertEqual(result["status"], "SERIALIZATION_ONLY")
        self.assertIn("<serial>", result["serialized_command"])

    def test_sanitized_text_removes_serial(self):
        sanitized = _safe_text("target emulator-5554 failed", "emulator-5554")
        self.assertNotIn("emulator-5554", sanitized)
        self.assertIn("<serial>", sanitized)

    @mock.patch("dev_tools.stage8a_device_acceptance.run_acceptance")
    def test_failure_report_masks_config_serial_and_adb_path(self, run_acceptance):
        def fail(args):
            args.resolved_serial = "emulator-5554"
            args.resolved_adb = "/private/tools/adb"
            raise subprocess.TimeoutExpired(
                ["/private/tools/adb", "-s", "emulator-5554", "get-state"],
                20,
            )

        run_acceptance.side_effect = fail
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "acceptance.json"
            exit_code = main(
                [
                    "--profile",
                    "alas",
                    "--serial-from-config",
                    "--report",
                    str(report),
                ]
            )
            payload = json.loads(report.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "FAIL")
        self.assertNotIn("emulator-5554", payload["error"])
        self.assertNotIn("/private/tools/adb", payload["error"])
        self.assertIn("<serial>", payload["error"])
        self.assertIn("<adb>", payload["error"])


if __name__ == "__main__":
    unittest.main()
