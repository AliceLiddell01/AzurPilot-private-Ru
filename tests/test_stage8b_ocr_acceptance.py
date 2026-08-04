from __future__ import annotations

import argparse
import os
import unittest
from unittest.mock import patch

import numpy as np

from dev_tools.stage8a_device_acceptance import AcceptanceFailure
from dev_tools.stage8b_ocr_acceptance import (
    _confirm_real_values,
    _decode_png,
    _provider_cache_snapshot,
    _registered_provider_evidence,
    _session_provider_evidence,
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

    def test_acceptance_does_not_enable_debug_or_provider_download_by_import(self) -> None:
        self.assertNotEqual(os.environ.get("AZURPILOT_OCR_DEBUG"), "1")
        self.assertNotEqual(os.environ.get("AZURPILOT_OCR_ALLOW_PROVIDER_DOWNLOAD"), "1")


if __name__ == "__main__":
    unittest.main()
