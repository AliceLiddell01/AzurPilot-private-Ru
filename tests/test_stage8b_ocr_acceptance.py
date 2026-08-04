from __future__ import annotations

import argparse
import os
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import numpy as np

from dev_tools.stage8a_device_acceptance import AcceptanceFailure
from dev_tools.stage8b_ocr_acceptance import (
    _confirm_real_values,
    _decode_png,
    _provider_cache_snapshot,
    _registered_provider_evidence,
    _session_provider_evidence,
    run_acceptance,
)


class Stage8BOcrAcceptanceTests(unittest.TestCase):
    def test_decode_png_returns_bgr_ndarray(self) -> None:
        import cv2

        image = np.zeros((8, 8, 3), dtype=np.uint8)
        ok, encoded = cv2.imencode(".png", image)
        self.assertTrue(ok)
        decoded = _decode_png(encoded.tobytes())
        self.assertEqual(decoded.shape, (8, 8, 3))

    def test_provider_evidence_distinguishes_registered_and_session(self) -> None:
        class Session:
            def get_providers(self):
                return ["CPUExecutionProvider"]

            def get_provider_options(self):
                return {"CPUExecutionProvider": {}}

        registered = _registered_provider_evidence()
        session = _session_provider_evidence(type("Model", (), {"session": Session()})())
        self.assertIn("available", registered)
        self.assertEqual(session["providers"], ["CPUExecutionProvider"])
        self.assertNotEqual(registered, session)

    def test_non_interactive_confirmation_requires_two_values(self) -> None:
        values = [
            {"id": 1, "category": "numeric", "value": "123", "score": 1.0, "box": []},
            {"id": 2, "category": "counter", "value": "1/2", "score": 1.0, "box": []},
        ]
        args = argparse.Namespace(non_interactive=True, confirmed_value_ids="1")
        with self.assertRaises(AcceptanceFailure):
            _confirm_real_values(values, args)

    def test_non_interactive_confirmation_records_selected_values(self) -> None:
        values = [
            {"id": 1, "category": "numeric", "value": "123", "score": 1.0, "box": []},
            {"id": 2, "category": "counter", "value": "1/2", "score": 1.0, "box": []},
            {"id": 3, "category": "stage", "value": "7-2", "score": 1.0, "box": []},
        ]
        args = argparse.Namespace(non_interactive=True, confirmed_value_ids="1,3")
        confirmed = _confirm_real_values(values, args)
        self.assertEqual([row["id"] for row in confirmed], [1, 3])

    def test_provider_cache_snapshot_is_machine_readable(self) -> None:
        with patch("dev_tools.stage8b_ocr_acceptance._provider_cache_paths", return_value=[]):
            self.assertEqual(_provider_cache_snapshot(), {})

    def test_cleanup_failure_still_restores_environment(self) -> None:
        temporary_root = Path(tempfile.mkdtemp(prefix="stage8b-acceptance-test-"))
        expected_values = {
            "AZURPILOT_OCR_DEBUG": "before-debug",
            "AZURPILOT_OCR_DEBUG_DIR": "before-directory",
            "AZURPILOT_OCR_ALLOW_PROVIDER_DOWNLOAD": "before-download",
        }
        previous_values = {
            name: os.environ.get(name)
            for name in expected_values
        }
        os.environ.update(expected_values)
        args = argparse.Namespace(
            profile="alas",
            serial="127.0.0.1:5555",
            serial_from_config=False,
            adb=None,
            expected_head=None,
            non_interactive=True,
            confirmed_value_ids="1,2",
        )
        details = {
            "server": "en",
            "backend": "onnxruntime",
            "device_preference": "cpu",
            "model_version": "auto",
            "vendor_ep_enabled": False,
        }
        patchers = (
            patch("dev_tools.stage8b_ocr_acceptance._validate_profile_name"),
            patch("dev_tools.stage8b_ocr_acceptance._git_head_sha", return_value="head"),
            patch(
                "dev_tools.stage8b_ocr_acceptance._load_profile",
                return_value={"package": "com.YoStarEN.AzurLane"},
            ),
            patch(
                "dev_tools.stage8b_ocr_acceptance._resolve_serial",
                return_value="127.0.0.1:5555",
            ),
            patch("dev_tools.stage8b_ocr_acceptance._resolve_adb", return_value="adb"),
            patch(
                "dev_tools.stage8b_ocr_acceptance._check_android_boot_completed",
                return_value={"boot_completed": True},
            ),
            patch(
                "dev_tools.stage8b_ocr_acceptance._detect_package",
                return_value="com.YoStarEN.AzurLane",
            ),
            patch(
                "dev_tools.stage8b_ocr_acceptance._load_ocr_config",
                return_value=(object(), details),
            ),
            patch("dev_tools.stage8b_ocr_acceptance._print_plan"),
            patch(
                "dev_tools.stage8b_ocr_acceptance._config_path",
                return_value=Path("config/alas.json"),
            ),
            patch("dev_tools.stage8b_ocr_acceptance._sha256", return_value="hash"),
            patch(
                "dev_tools.stage8b_ocr_acceptance._provider_cache_snapshot",
                return_value={},
            ),
            patch(
                "dev_tools.stage8b_ocr_acceptance._child_process_snapshot",
                return_value={},
            ),
            patch(
                "dev_tools.stage8b_ocr_acceptance._environment_fingerprint",
                return_value={},
            ),
            patch(
                "dev_tools.stage8b_ocr_acceptance._registered_provider_evidence",
                return_value={},
            ),
            patch(
                "dev_tools.stage8b_ocr_acceptance.tempfile.mkdtemp",
                return_value=str(temporary_root),
            ),
            patch(
                "dev_tools.stage8b_ocr_acceptance._run_adb",
                side_effect=RuntimeError("body failure"),
            ),
            patch("dev_tools.stage8b_ocr_acceptance.cleanup_debug_directory"),
            patch(
                "dev_tools.stage8b_ocr_acceptance.shutil.rmtree",
                side_effect=OSError("cleanup failure"),
            ),
        )
        try:
            with ExitStack() as stack:
                for patcher in patchers:
                    stack.enter_context(patcher)
                stack.enter_context(
                    self.assertRaisesRegex(
                        AcceptanceFailure,
                        "Не удалось безопасно очистить временные OCR-данные",
                    )
                )
                run_acceptance(args)
            for name, value in expected_values.items():
                self.assertEqual(os.environ.get(name), value)
        finally:
            shutil.rmtree(temporary_root, ignore_errors=True)
            for name, value in previous_values.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def test_acceptance_does_not_enable_debug_or_provider_download_by_import(self) -> None:
        self.assertNotEqual(os.environ.get("AZURPILOT_OCR_DEBUG"), "1")
        self.assertNotEqual(os.environ.get("AZURPILOT_OCR_ALLOW_PROVIDER_DOWNLOAD"), "1")


if __name__ == "__main__":
    unittest.main()
