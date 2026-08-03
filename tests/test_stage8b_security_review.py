from __future__ import annotations

import os
import pickle
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from dev_tools.stage8b_security_audit import build_security_review
from module.ocr.stage8b_privacy import (
    OcrDebugOutputError, cleanup_debug_directory, save_debug_image,
)
from module.ocr.stage8b_rpc_security import (
    OcrRpcSecurityError, client_uri, decode_trusted_local_image,
    loopback_bind_uri, normalize_loopback_address,
)


class Stage8BSecurityReviewTests(unittest.TestCase):
    def test_rpc_is_loopback_only(self) -> None:
        self.assertEqual(normalize_loopback_address("localhost:22268"), "127.0.0.1:22268")
        self.assertEqual(client_uri("127.0.0.1:22268"), "tcp://127.0.0.1:22268")
        self.assertEqual(loopback_bind_uri(22268), "tcp://127.0.0.1:22268")
        for address in ("0.0.0.0:22268", "*:22268", "192.0.2.1:22268"):
            with self.assertRaises(OcrRpcSecurityError):
                normalize_loopback_address(address)

    def test_trusted_local_payload_is_bounded_and_typed(self) -> None:
        image = np.zeros((8, 8, 3), dtype=np.uint8)
        decoded = decode_trusted_local_image(pickle.dumps(image))
        self.assertEqual(decoded.shape, image.shape)
        with self.assertRaises(OcrRpcSecurityError):
            decode_trusted_local_image(pickle.dumps("not-an-array"))
        with self.assertRaises(OcrRpcSecurityError):
            decode_trusted_local_image(b"broken")

    def test_debug_output_is_opt_in_and_safe(self) -> None:
        image = np.zeros((8, 8, 3), dtype=np.uint8)
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"AZURPILOT_OCR_DEBUG": "0"}, clear=False):
                self.assertIsNone(save_debug_image(image, model_name="azur_lane", directory=directory))
            with patch.dict(os.environ, {"AZURPILOT_OCR_DEBUG": "1"}, clear=False):
                path = save_debug_image(image, model_name="azur_lane", directory=directory)
            self.assertIsNotNone(path)
            assert path is not None
            self.assertTrue(path.is_file())
            self.assertNotIn("Operation Siren", path.name)
            cleanup_debug_directory(directory)
            self.assertFalse(Path(directory).exists())

    def test_debug_output_rejects_git_root(self) -> None:
        image = np.zeros((8, 8, 3), dtype=np.uint8)
        with patch.dict(os.environ, {"AZURPILOT_OCR_DEBUG": "1"}, clear=False):
            with self.assertRaises(OcrDebugOutputError):
                save_debug_image(image, model_name="azur_lane", directory="ocr_debug")

    def test_machine_readable_security_review_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload, metrics = build_security_review(Path(directory))
        self.assertEqual(payload["status"], "PASS")
        self.assertTrue(all(value == 0 for value in metrics.values()), metrics)


if __name__ == "__main__":
    unittest.main()
