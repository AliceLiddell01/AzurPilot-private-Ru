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
    _check_reconnect,
    _command_evidence,
    _external_backend_evidence,
    _git_head_sha,
    _is_network_serial,
    _resolve_serial,
    _run_adb,
    _run_adb_connect,
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

    @mock.patch("dev_tools.stage8a_device_acceptance.subprocess.run")
    def test_adb_connect_is_endpoint_explicit(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        _run_adb_connect("adb", "127.0.0.1:16416")
        self.assertEqual(run.call_args.args[0], ["adb", "connect", "127.0.0.1:16416"])

    def test_network_serial_detection_requires_valid_host_port(self):
        self.assertTrue(_is_network_serial("127.0.0.1:16416"))
        self.assertTrue(_is_network_serial("[::1]:5555"))
        self.assertFalse(_is_network_serial("emulator-5554"))
        self.assertFalse(_is_network_serial("127.0.0.1:99999"))

    @mock.patch("dev_tools.stage8a_device_acceptance._wait_for_target_device")
    @mock.patch("dev_tools.stage8a_device_acceptance._run_adb_connect")
    @mock.patch("dev_tools.stage8a_device_acceptance._run_adb")
    @mock.patch("dev_tools.stage8a_device_acceptance._confirm", return_value=True)
    def test_tcp_reconnect_runs_explicit_connect_for_same_target(
        self,
        confirm,
        run_adb,
        run_adb_connect,
        wait_for_target,
    ):
        del confirm
        run_adb.return_value = subprocess.CompletedProcess(
            [], 0, stdout="reconnecting\n", stderr=""
        )
        run_adb_connect.return_value = subprocess.CompletedProcess(
            [], 0, stdout="connected to 127.0.0.1:16416\n", stderr=""
        )
        wait_for_target.return_value = {
            "restored": True,
            "attempts": 2,
            "last_state": {"returncode": 0, "stdout": "device\n", "stderr": ""},
        }

        result = _check_reconnect("adb", "127.0.0.1:16416", False)

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["recovery_mode"], "explicit_tcp_connect")
        self.assertTrue(result["transport_restored"])
        run_adb.assert_called_once_with(
            "adb", "127.0.0.1:16416", "reconnect", timeout=30
        )
        run_adb_connect.assert_called_once_with(
            "adb", "127.0.0.1:16416", timeout=30
        )
        wait_for_target.assert_called_once_with("adb", "127.0.0.1:16416")
        self.assertNotIn("127.0.0.1:16416", result["explicit_connect"]["stdout"])
        self.assertIn("<serial>", result["explicit_connect"]["stdout"])

    @mock.patch("dev_tools.stage8a_device_acceptance._wait_for_target_device")
    @mock.patch("dev_tools.stage8a_device_acceptance._run_adb_connect")
    @mock.patch("dev_tools.stage8a_device_acceptance._run_adb")
    @mock.patch("dev_tools.stage8a_device_acceptance._confirm", return_value=True)
    def test_local_emulator_reconnect_does_not_run_adb_connect(
        self,
        confirm,
        run_adb,
        run_adb_connect,
        wait_for_target,
    ):
        del confirm
        run_adb.return_value = subprocess.CompletedProcess(
            [], 0, stdout="reconnecting\n", stderr=""
        )
        wait_for_target.return_value = {
            "restored": True,
            "attempts": 1,
            "last_state": {"returncode": 0, "stdout": "device\n", "stderr": ""},
        }

        result = _check_reconnect("adb", "emulator-5554", False)

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["recovery_mode"], "target_reconnect")
        run_adb_connect.assert_not_called()

    @mock.patch("dev_tools.stage8a_device_acceptance._wait_for_target_device")
    @mock.patch("dev_tools.stage8a_device_acceptance._run_adb_connect")
    @mock.patch("dev_tools.stage8a_device_acceptance._run_adb")
    @mock.patch("dev_tools.stage8a_device_acceptance._confirm", return_value=True)
    def test_tcp_reconnect_remains_fail_closed_when_target_never_returns(
        self,
        confirm,
        run_adb,
        run_adb_connect,
        wait_for_target,
    ):
        del confirm
        run_adb.return_value = subprocess.CompletedProcess(
            [], 0, stdout="reconnecting\n", stderr=""
        )
        run_adb_connect.return_value = subprocess.CompletedProcess(
            [], 1, stdout="failed to connect\n", stderr=""
        )
        wait_for_target.return_value = {
            "restored": False,
            "attempts": 60,
            "last_state": {"returncode": 1, "stdout": "", "stderr": "offline"},
        }

        with self.assertRaises(AcceptanceFailure):
            _check_reconnect("adb", "127.0.0.1:16416", False)

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


    @mock.patch("dev_tools.stage8a_device_acceptance.subprocess.run")
    def test_git_head_sha_is_exact_and_shell_free(self, run):
        run.return_value = subprocess.CompletedProcess(
            [],
            0,
            stdout="a" * 40 + "\n",
            stderr="",
        )
        self.assertEqual(_git_head_sha(), "a" * 40)
        command = run.call_args.args[0]
        self.assertEqual(command, ["git", "rev-parse", "HEAD"])
        self.assertNotIn("shell", run.call_args.kwargs)

    @mock.patch("dev_tools.stage8a_device_acceptance.subprocess.run")
    def test_git_head_sha_fails_closed(self, run):
        run.return_value = subprocess.CompletedProcess(
            [],
            0,
            stdout="not-a-sha\n",
            stderr="",
        )
        with self.assertRaises(AcceptanceFailure):
            _git_head_sha()

    def test_external_backend_evidence_separates_real_and_handshake_levels(self):
        report = {
            "screenshot_backend": "nemu_ipc",
            "control_backend": "minitouch",
            "live_preview": {
                "mode": "screenshot_fallback",
                "scrcpy": {"reason": "no_frame_after_handshake"},
            },
            "control": {
                "configured_backend": {
                    "backend": "minitouch",
                    "probe": "handshake_without_touch",
                }
            },
        }
        evidence = {row["backend"]: row for row in _external_backend_evidence(report)}
        self.assertEqual(evidence["ADB"]["level"], "REAL_ACCEPTANCE")
        self.assertEqual(evidence["nemu_ipc"]["level"], "REAL_ACCEPTANCE")
        self.assertEqual(evidence["minitouch"]["level"], "REAL_ACCEPTANCE_HANDSHAKE")
        self.assertEqual(evidence["scrcpy"]["level"], "HANDSHAKE_ONLY")
        self.assertIn("no_frame_after_handshake", evidence["scrcpy"]["limitations"])

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
