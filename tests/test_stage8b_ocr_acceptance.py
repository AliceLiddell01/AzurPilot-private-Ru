from __future__ import annotations

import os
import unittest

import numpy as np

from dev_tools.stage8b_ocr_acceptance import _decode_png, _provider_evidence


class Stage8BOcrAcceptanceTests(unittest.TestCase):
    def test_decode_png_returns_bgr_ndarray(self) -> None:
        import cv2
        image = np.zeros((8, 8, 3), dtype=np.uint8)
        ok, encoded = cv2.imencode(".png", image)
        self.assertTrue(ok)
        decoded = _decode_png(encoded.tobytes())
        self.assertEqual(decoded.shape, (8, 8, 3))

    def test_provider_evidence_uses_concrete_session(self) -> None:
        class Session:
            def get_providers(self):
                return ["CPUExecutionProvider"]
            def get_provider_options(self):
                return {"CPUExecutionProvider": {}}
        evidence = _provider_evidence(type("Model", (), {"session": Session()})())
        self.assertEqual(evidence["session"], ["CPUExecutionProvider"])

    def test_acceptance_does_not_enable_debug_or_provider_download_by_import(self) -> None:
        self.assertNotEqual(os.environ.get("AZURPILOT_OCR_DEBUG"), "1")
        self.assertNotEqual(os.environ.get("AZURPILOT_OCR_ALLOW_PROVIDER_DOWNLOAD"), "1")


if __name__ == "__main__":
    unittest.main()
