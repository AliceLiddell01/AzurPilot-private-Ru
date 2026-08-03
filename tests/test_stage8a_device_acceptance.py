from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from dev_tools.stage8a_device_acceptance import (
    AcceptanceFailure,
    _check_configured_control_backend,
    _check_control,
    _check_preview,
    _command_evidence,
    _resolve_serial,
    _run_adb,
    _safe_text,
    main,
)


class _FakeSession:
    def __init__(self, chunks):
        self.alive = True
        self.resolution = (640, 360)
        self._chunks = iter(chunks)

    def read_video(self):
        return next(self._chunks, b"")


class _FakeLiveScrcpySession:
    session = None
    released = False

    @classmethod
    def acquire(cls, profile, fps, width, bitrate_scale):
        del profile, fps, width, bitrate_scale
        return cls.session

    @classmethod
    def release(cls, profile, session=None):
        del profile, session
        cls.released = True


class _FakeScreenshotDevice:
    def __init__(self, image):
        self.image = image
        self.calls = 0

    def screenshot(self):
        self.calls += 1
        return self.image.copy()


class _FakeMinitouchClient:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeMinitouchDevice:
    def __init__(self):
        self.max_x = 1080
        self.max_y = 1920
        self._minitouch_port = 12345
        self._minitouch_client = _FakeMinitouchClient()
        self.initialized = False
        self.removed = []

    def minitouch_init(self):
        self.initialized = True

    def adb_forward_remove(self, remote):
        self.removed.append(remote)


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

    @mock.patch("dev_tools.stage8a_device_acceptance._preview_dependencies")
    def test_preview_accepts_raw_scrcpy_after_initial_timeout(self, dependencies):
        _FakeLiveScrcpySession.session = _FakeSession([None, b"\x00\x00\x00\x01frame"])
        _FakeLiveScrcpySession.released = False
        dependencies.return_value = (
            _FakeLiveScrcpySession,
            lambda: "ffmpeg",
            mock.Mock(),
        )

        result = _check_preview("alas", "nemu_ipc")

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["mode"], "scrcpy")
        self.assertGreater(result["first_video_chunk_bytes"], 0)
        self.assertTrue(_FakeLiveScrcpySession.released)

    @mock.patch("dev_tools.stage8a_device_acceptance._preview_dependencies")
    def test_preview_uses_configured_screenshot_fallback_when_scrcpy_has_no_frame(
        self,
        dependencies,
    ):
        image = np.zeros((360, 640, 3), dtype=np.uint8)
        device = _FakeScreenshotDevice(image)
        _FakeLiveScrcpySession.session = _FakeSession([b""])
        _FakeLiveScrcpySession.released = False
        dependencies.return_value = (
            _FakeLiveScrcpySession,
            lambda: "ffmpeg",
            lambda profile: (device, image.copy()),
        )

        result = _check_preview("alas", "nemu_ipc")

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["mode"], "screenshot_fallback")
        self.assertEqual(result["configured_screenshot_backend"], "nemu_ipc")
        self.assertEqual(result["frames_verified"], 2)
        self.assertEqual(result["scrcpy"]["reason"], "no_frame_after_handshake")
        self.assertEqual(device.calls, 1)
        self.assertTrue(_FakeLiveScrcpySession.released)

    @mock.patch("dev_tools.stage8a_device_acceptance._preview_dependencies")
    def test_preview_fails_when_scrcpy_and_webui_fallback_are_unavailable(self, dependencies):
        _FakeLiveScrcpySession.session = _FakeSession([b""])
        dependencies.return_value = (
            _FakeLiveScrcpySession,
            lambda: None,
            mock.Mock(),
        )

        with self.assertRaises(AcceptanceFailure):
            _check_preview("alas", "nemu_ipc")

    @mock.patch("dev_tools.stage8a_device_acceptance._new_device")
    def test_minitouch_backend_probe_performs_handshake_without_touch(self, new_device):
        device = _FakeMinitouchDevice()
        new_device.return_value = device

        result = _check_configured_control_backend("alas", "minitouch")

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["probe"], "handshake_without_touch")
        self.assertTrue(device.initialized)
        self.assertTrue(device._minitouch_client.closed)
        self.assertEqual(device.removed, ["tcp:12345"])

    @mock.patch("dev_tools.stage8a_device_acceptance._check_configured_control_backend")
    @mock.patch("dev_tools.stage8a_device_acceptance._run_adb")
    def test_noninteractive_control_never_sends_input(self, run_adb, backend_probe):
        backend_probe.return_value = {
            "status": "PASS",
            "backend": "minitouch",
            "probe": "handshake_without_touch",
        }
        result = _check_control(
            "alas",
            "minitouch",
            "adb",
            "emulator-5554",
            non_interactive=True,
        )
        self.assertEqual(result["status"], "SERIALIZATION_ONLY")
        self.assertIn("<serial>", result["serialized_command"])
        run_adb.assert_not_called()

    def test_sanitized_text_removes_serial(self):
        sanitized = _safe_text("target emulator-5554 failed", "emulator-5554")
        self.assertNotIn("emulator-5554", sanitized)
        self.assertIn("<serial>", sanitized)

    def test_sanitized_text_redacts_credentials_hosts_paths_and_html(self):
        token_prefix = "gh" + "p_"
        token_value = token_prefix + ("A" * 26)
        private_key_begin = "-----" + "BEGIN OPENSSH PRIVATE KEY-----"
        private_key_end = "-----" + "END OPENSSH PRIVATE KEY-----"
        sensitive = (
            "\x1b[31mAuthorization: Bearer top-secret\x1b[0m\n"
            "url=https://alice:pass123@example.invalid/path\n"
            f"{token_value}\n"
            "password=hunter2 token: abcdef api_key XYZ secret value\n"
            "ssh alice@example.invalid:/home/alice/private\n"
            "traceback C:\\Users\\Alice\\project\\main.py "
            "/home/alice/project/main.py\n"
            "target emulator-5554 host 10.0.0.5:5555 localhost:7912\n"
            "<script>alert(1)</script>\n"
            f"{private_key_begin}\n"
            "private-key-material\n"
            f"{private_key_end}\n"
        )
        sanitized = _safe_text(sensitive, "emulator-5554")

        for forbidden in (
            "top-secret",
            "pass123",
            "ghp_",
            "hunter2",
            "abcdef",
            "XYZ",
            "alice@example.invalid",
            "Alice",
            "10.0.0.5",
            "localhost",
            "<script>",
            "</script>",
            "private-key-material",
            "\x1b",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, sanitized)
        for marker in (
            "<credential>",
            "<token>",
            "<ssh-location>",
            "<path>",
            "<serial>",
            "<host>",
            "<html-redacted>",
            "<private-key>",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, sanitized)

    def test_binary_command_evidence_records_only_byte_counts(self):
        result = subprocess.CompletedProcess(
            [],
            1,
            stdout=b"\x89PNG\r\n\x1a\nsecret-binary",
            stderr=b"\x00\x01\x02",
        )
        evidence = _command_evidence(result, "emulator-5554")
        self.assertEqual(evidence["stdout"], "<binary:21 bytes>")
        self.assertEqual(evidence["stderr"], "<binary:3 bytes>")

    def test_sanitized_text_bounds_external_output_and_removes_controls(self):
        sanitized = _safe_text("prefix\x00" + ("x" * 20_000))
        self.assertNotIn("\x00", sanitized)
        self.assertLessEqual(len(sanitized), 16_384 + len("\n<truncated>"))
        self.assertTrue(sanitized.endswith("\n<truncated>"))

    @mock.patch("dev_tools.stage8a_device_acceptance.run_acceptance")
    def test_failure_report_masks_config_serial_and_adb_path(self, run_acceptance):
        def fail(args):
            args.resolved_serial = "emulator-5554"
            args.resolved_adb = "/private/tools/adb"
            args.partial_report = {
                "status": "RUNNING",
                "screenshot": {"color_contract": "BGR"},
            }
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
        self.assertEqual(payload["screenshot"]["color_contract"], "BGR")
        self.assertNotIn("emulator-5554", payload["error"])
        self.assertNotIn("/private/tools/adb", payload["error"])
        self.assertIn("<serial>", payload["error"])
        self.assertIn("<adb>", payload["error"])


if __name__ == "__main__":
    unittest.main()
