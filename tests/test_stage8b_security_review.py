from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from dev_tools.stage8b_security_audit import build_security_review
from module.ocr.rpc import (
    MAX_CANDIDATE_ALPHABET_LENGTH,
    MAX_RPC_BATCH_IMAGES,
    ModelProxy,
    _get_server_model,
    _validate_batch,
    _validate_candidate_alphabet,
)
from module.ocr.stage8b_privacy import (
    OcrDebugOutputError,
    cleanup_debug_directory,
    save_debug_image,
)
from module.ocr.stage8b_rpc_security import (
    OcrRpcSecurityError,
    client_uri,
    decode_image_payload,
    encode_image_payload,
    loopback_bind_uri,
    normalize_loopback_address,
)


class _FallbackModel:
    def __init__(self, result: str):
        self.result = result
        self.calls: list[object] = []

    def ocr(self, image):
        self.calls.append(image)
        return self.result


class Stage8BSecurityReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self._rpc_client = ModelProxy.client
        self._rpc_online = ModelProxy.online

    def tearDown(self) -> None:
        ModelProxy.client = self._rpc_client
        ModelProxy.online = self._rpc_online

    def test_rpc_is_loopback_only_and_uses_one_canonical_bind(self) -> None:
        self.assertEqual(
            normalize_loopback_address("localhost:22268"),
            "127.0.0.1:22268",
        )
        self.assertEqual(
            normalize_loopback_address("[::1]:22268"),
            "127.0.0.1:22268",
        )
        self.assertEqual(
            client_uri("127.0.0.1:22268"),
            "tcp://127.0.0.1:22268",
        )
        self.assertEqual(loopback_bind_uri(22268), "tcp://127.0.0.1:22268")
        for address in ("0.0.0.0:22268", "*:22268", "192.0.2.1:22268"):
            with self.assertRaises(OcrRpcSecurityError):
                normalize_loopback_address(address)

    def test_payload_round_trip_preserves_array_without_pickle(self) -> None:
        for image in (
            np.arange(64, dtype=np.uint8).reshape(8, 8),
            np.arange(8 * 8 * 3, dtype=np.float32).reshape(8, 8, 3),
        ):
            payload = encode_image_payload(image)
            decoded = decode_image_payload(payload)
            self.assertEqual(decoded.shape, image.shape)
            self.assertEqual(decoded.dtype, image.dtype)
            np.testing.assert_array_equal(decoded, image)

    def test_payload_rejects_unknown_truncated_and_mismatched_data(self) -> None:
        image = np.zeros((8, 8, 3), dtype=np.uint8)
        payload = encode_image_payload(image)
        for invalid in (b"broken", payload[:-1], payload + b"extra"):
            with self.assertRaises(OcrRpcSecurityError):
                decode_image_payload(invalid)
        with self.assertRaises(OcrRpcSecurityError):
            encode_image_payload(np.array([object()], dtype=object))

    def test_offline_rpc_fallback_does_not_serialize_input(self) -> None:
        marker = object()
        fallback = _FallbackModel("local")
        proxy = ModelProxy("azur_lane")
        ModelProxy.online = False
        ModelProxy.client = None

        with patch(
            "module.ocr.rpc._get_local_model",
            return_value=fallback,
        ), patch(
            "module.ocr.rpc.encode_image_payload",
            side_effect=AssertionError("offline path serialized input"),
        ):
            self.assertEqual(proxy.ocr(marker), "local")

        self.assertEqual(fallback.calls, [marker])

    def test_rpc_transport_failure_switches_process_to_local_fallback(self) -> None:
        image = np.zeros((4, 4, 3), dtype=np.uint8)
        fallback = _FallbackModel("fallback")
        proxy = ModelProxy("azur_lane")
        ModelProxy.online = True

        def fail(*_args):
            raise RuntimeError("fixture transport failure")

        proxy.client = fail
        with patch("module.ocr.rpc._get_local_model", return_value=fallback):
            self.assertEqual(proxy.ocr(image), "fallback")

        self.assertFalse(ModelProxy.online)
        np.testing.assert_array_equal(fallback.calls[0], image)

    def test_rpc_model_allowlist_rejects_attribute_traversal(self) -> None:
        for name in ("__class__", "hello", "close", "missing"):
            with self.assertRaises(ValueError):
                ModelProxy(name)
            with self.assertRaises(ValueError):
                _get_server_model(SimpleNamespace(), name)

    def test_rpc_batch_and_alphabet_limits_are_enforced(self) -> None:
        self.assertEqual(_validate_batch([1]), [1])
        with self.assertRaises(ValueError):
            _validate_batch([])
        with self.assertRaises(ValueError):
            _validate_batch([None] * (MAX_RPC_BATCH_IMAGES + 1))
        with self.assertRaises(ValueError):
            _validate_batch("not-a-batch")

        self.assertIsNone(_validate_candidate_alphabet(None))
        self.assertEqual(_validate_candidate_alphabet("ABC"), "ABC")
        with self.assertRaises(ValueError):
            _validate_candidate_alphabet(123)
        with self.assertRaises(ValueError):
            _validate_candidate_alphabet(
                "A" * (MAX_CANDIDATE_ALPHABET_LENGTH + 1)
            )

    def test_debug_output_is_opt_in_and_safe(self) -> None:
        image = np.zeros((8, 8, 3), dtype=np.uint8)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "debug"
            with patch.dict(
                os.environ,
                {"AZURPILOT_OCR_DEBUG": "0"},
                clear=False,
            ):
                self.assertIsNone(
                    save_debug_image(
                        image,
                        model_name="azur_lane",
                        directory=target,
                    )
                )
            with patch.dict(
                os.environ,
                {"AZURPILOT_OCR_DEBUG": "1"},
                clear=False,
            ):
                path = save_debug_image(
                    image,
                    model_name="azur_lane",
                    directory=target,
                )
            self.assertIsNotNone(path)
            assert path is not None
            self.assertTrue(path.is_file())
            self.assertNotIn("Operation Siren", path.name)
            self.assertFalse(
                any(entry.name.startswith(".ocr-") for entry in target.iterdir())
            )
            cleanup_debug_directory(target)
            self.assertFalse(target.exists())

    def test_debug_output_rejects_git_root_and_symlink_components(self) -> None:
        image = np.zeros((8, 8, 3), dtype=np.uint8)
        with patch.dict(
            os.environ,
            {"AZURPILOT_OCR_DEBUG": "1"},
            clear=False,
        ):
            with self.assertRaises(OcrDebugOutputError):
                save_debug_image(
                    image,
                    model_name="azur_lane",
                    directory="ocr_debug",
                )
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                real = root / "real"
                real.mkdir()
                link = root / "link"
                try:
                    link.symlink_to(real, target_is_directory=True)
                except (OSError, NotImplementedError):
                    self.skipTest("Symlink creation is unavailable")
                with self.assertRaises(OcrDebugOutputError):
                    save_debug_image(
                        image,
                        model_name="azur_lane",
                        directory=link / "debug",
                    )

    def test_machine_readable_security_review_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload, metrics = build_security_review(Path(directory))
        self.assertEqual(payload["status"], "PASS")
        self.assertTrue(all(value == 0 for value in metrics.values()), metrics)


if __name__ == "__main__":
    unittest.main()
