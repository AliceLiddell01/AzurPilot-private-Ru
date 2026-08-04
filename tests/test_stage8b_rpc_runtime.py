from __future__ import annotations

import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from module.ocr.rpc import (
    MAX_CANDIDATE_ALPHABET_LENGTH,
    MAX_RPC_BATCH_IMAGES,
    ModelProxy,
    _get_server_model,
    _validate_batch,
    _validate_candidate_alphabet,
)


class _FallbackModel:
    def __init__(self, result: str):
        self.result = result
        self.calls: list[object] = []

    def ocr(self, image):
        self.calls.append(image)
        return self.result


class Stage8BRpcRuntimeTests(unittest.TestCase):
    @staticmethod
    def _models_module(model: _FallbackModel):
        module = types.ModuleType("module.ocr.models")
        module.OCR_MODEL = SimpleNamespace(azur_lane=model)
        return module

    def test_offline_public_ocr_skips_serialization_and_uses_local_model(self) -> None:
        marker = object()
        fallback = _FallbackModel("local")
        proxy = ModelProxy("azur_lane")
        proxy.online = False
        proxy.client = None

        with patch.dict(
            sys.modules,
            {"module.ocr.models": self._models_module(fallback)},
        ):
            self.assertEqual(proxy.ocr(marker), "local")

        self.assertEqual(fallback.calls, [marker])

    def test_rpc_failure_switches_instance_to_local_fallback(self) -> None:
        image = np.zeros((4, 4, 3), dtype=np.uint8)
        fallback = _FallbackModel("fallback")
        proxy = ModelProxy("azur_lane")
        proxy.online = True

        def fail(*_args):
            raise RuntimeError("fixture transport failure")

        proxy.client = fail
        with patch.dict(
            sys.modules,
            {"module.ocr.models": self._models_module(fallback)},
        ):
            self.assertEqual(proxy.ocr(image), "fallback")

        self.assertFalse(proxy.online)
        np.testing.assert_array_equal(fallback.calls[0], image)

    def test_rpc_args_factory_is_lazy_while_offline(self) -> None:
        proxy = ModelProxy("azur_lane")
        proxy.online = False

        def forbidden_factory():
            self.fail("RPC argument factory was evaluated while offline")

        self.assertEqual(
            proxy._rpc_or_fallback("ocr", lambda: "local", forbidden_factory),
            "local",
        )

    def test_model_allowlist_rejects_attribute_traversal(self) -> None:
        for name in ("__class__", "hello", "close", "missing"):
            with self.assertRaises(ValueError):
                ModelProxy(name)
            with self.assertRaises(ValueError):
                _get_server_model(SimpleNamespace(), name)

    def test_batch_and_alphabet_limits_are_enforced(self) -> None:
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
            _validate_candidate_alphabet("A" * (MAX_CANDIDATE_ALPHABET_LENGTH + 1))


if __name__ == "__main__":
    unittest.main()
