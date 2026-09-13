from __future__ import annotations

import json
import socket
import sys
import threading
import time
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import zmq

import module.ocr.rpc as ocr_rpc
from module.ocr.rpc import (
    MAX_CANDIDATE_ALPHABET_LENGTH,
    MAX_RPC_BATCH_BYTES,
    MAX_RPC_BATCH_IMAGES,
    RPC_PROTOCOL_VERSION,
    ModelProxy,
    OcrRpcRemoteError,
    OcrRpcTransportError,
    _encode_batch,
    _encode_call,
    _get_server_model,
    _OcrRpcServer,
    _OcrRpcService,
    _validate_batch,
    _validate_candidate_alphabet,
    _ZmqRpcClient,
)
from module.ocr.rpc_security import OcrRpcSecurityError


class _FallbackModel:
    def __init__(self, result: str):
        self.result = result
        self.calls: list[object] = []

    def ocr(self, image):
        self.calls.append(image)
        return self.result


class _RpcModel:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.delay = 0.0
        self.fail_ocr = False

    def _before(self, method: str, *args) -> None:
        self.calls.append((method, args))
        if self.delay:
            time.sleep(self.delay)
        if self.fail_ocr and method == "ocr":
            raise RuntimeError("ошибка сервера в фикстуре")

    def ocr(self, image):
        self._before("ocr", image)
        return "ocr"

    def ocr_for_single_line(self, image):
        self._before("ocr_for_single_line", image)
        return "single_line"

    def ocr_for_single_lines(self, images):
        self._before("ocr_for_single_lines", images)
        return ["single_lines", len(images)]

    def set_cand_alphabet(self, cand_alphabet):
        self._before("set_cand_alphabet", cand_alphabet)
        return cand_alphabet

    def atomic_ocr(self, image, cand_alphabet=None):
        self._before("atomic_ocr", image, cand_alphabet)
        return "atomic"

    def atomic_ocr_for_single_line(self, image, cand_alphabet=None):
        self._before("atomic_ocr_for_single_line", image, cand_alphabet)
        return "atomic_single_line"

    def atomic_ocr_for_single_lines(self, images, cand_alphabet=None):
        self._before("atomic_ocr_for_single_lines", images, cand_alphabet)
        return ["atomic_single_lines", len(images)]

    def debug(self, images):
        self._before("debug", images)
        return {"count": len(images), "raw": b"debug", "image": images[0]}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _raw_request(endpoint: str, frames: list[bytes]) -> list[bytes]:
    context = zmq.Context()
    socket_ = context.socket(zmq.DEALER)
    socket_.setsockopt(zmq.LINGER, 0)
    socket_.setsockopt(zmq.RCVTIMEO, 2000)
    try:
        socket_.connect(endpoint)
        socket_.send_multipart(frames)
        return socket_.recv_multipart()
    finally:
        socket_.close(linger=0)
        context.term()


class OcrRpcRuntimeTests(unittest.TestCase):
    @staticmethod
    def _start_server(model: _RpcModel):
        port = _free_port()
        stop_event = threading.Event()
        ready_event = threading.Event()
        service = _OcrRpcService(SimpleNamespace(azur_lane=model))
        server = _OcrRpcServer(port, service)
        thread = threading.Thread(
            target=server.run,
            kwargs={"stop_event": stop_event, "ready_event": ready_event},
            daemon=True,
        )
        thread.start()
        if not ready_event.wait(3):
            stop_event.set()
            thread.join(3)
            raise AssertionError("локальный OCR RPC-сервер не сообщил о готовности")
        return port, service, stop_event, thread

    @staticmethod
    def _stop_server(stop_event: threading.Event, thread: threading.Thread) -> None:
        stop_event.set()
        thread.join(3)
        if thread.is_alive():
            raise AssertionError("локальный OCR RPC-сервер не остановился")

    @staticmethod
    def _models_module(model: _FallbackModel):
        module = types.ModuleType("module.ocr.models")
        module.OCR_MODEL = SimpleNamespace(azur_lane=model)
        return module

    def test_local_ipc_exercises_all_methods_and_binary_results(self) -> None:
        image = np.arange(48, dtype=np.uint8).reshape((4, 4, 3))
        images = [image, image + 1]
        model = _RpcModel()
        port, service, stop_event, thread = self._start_server(model)
        client = _ZmqRpcClient(f"127.0.0.1:{port}", timeout=1)
        try:
            self.assertEqual(client.hello(), "hello")
            self.assertEqual(client("ocr", "azur_lane", image), "ocr")
            self.assertEqual(
                client("ocr_for_single_line", "azur_lane", image),
                "single_line",
            )
            self.assertEqual(
                client("ocr_for_single_lines", "azur_lane", images),
                ["single_lines", 2],
            )
            self.assertEqual(
                client("set_cand_alphabet", "azur_lane", "ABC"),
                "ABC",
            )
            self.assertEqual(
                client("atomic_ocr", "azur_lane", image, "ABC"),
                "atomic",
            )
            self.assertEqual(
                client("atomic_ocr_for_single_line", "azur_lane", image, None),
                "atomic_single_line",
            )
            self.assertEqual(
                client("atomic_ocr_for_single_lines", "azur_lane", images, "ABC"),
                ["atomic_single_lines", 2],
            )
            debug = client("debug", "azur_lane", images)
            self.assertEqual(debug["count"], 2)
            self.assertEqual(debug["raw"], b"debug")
            np.testing.assert_array_equal(debug["image"], image)

            proxy = ModelProxy("azur_lane")
            proxy.client = client
            proxy.online = True
            self.assertEqual(proxy.ocr(image), "ocr")
            for method, args in model.calls:
                if method in {
                    "ocr",
                    "ocr_for_single_line",
                    "atomic_ocr",
                    "atomic_ocr_for_single_line",
                }:
                    self.assertIsInstance(args[0], np.ndarray)
                elif method in {
                    "ocr_for_single_lines",
                    "atomic_ocr_for_single_lines",
                    "debug",
                }:
                    self.assertTrue(
                        all(isinstance(item, np.ndarray) for item in args[0])
                    )
            self.assertEqual(service.metrics.snapshot()["errors"], 0)
        finally:
            client.close()
            self._stop_server(stop_event, thread)

    def test_malformed_oversized_and_server_errors_are_returned(self) -> None:
        image = np.zeros((4, 4, 3), dtype=np.uint8)
        model = _RpcModel()
        port, service, stop_event, thread = self._start_server(model)
        endpoint = f"tcp://127.0.0.1:{port}"
        client = _ZmqRpcClient(f"127.0.0.1:{port}", timeout=1)
        try:
            control, _ = _encode_call("ocr", "azur_lane", (image,))
            malformed = json.loads(control.decode("utf-8"))
            response = _raw_request(
                endpoint,
                [
                    json.dumps(malformed, separators=(",", ":")).encode("utf-8"),
                    b"not-an-image-payload",
                ],
            )
            response_control = json.loads(response[0].decode("utf-8"))
            self.assertFalse(response_control["ok"])
            self.assertEqual(response_control["error"]["type"], "validation")

            oversized = {
                "version": RPC_PROTOCOL_VERSION,
                "kind": "request",
                "request_id": "oversized-batch",
                "method": "ocr_for_single_lines",
                "lang": "azur_lane",
                "args": [
                    {
                        "kind": "list",
                        "items": [
                            {"kind": "scalar", "value": None}
                            for _ in range(MAX_RPC_BATCH_IMAGES + 1)
                        ],
                    }
                ],
            }
            response = _raw_request(
                endpoint,
                [json.dumps(oversized, separators=(",", ":")).encode("utf-8")],
            )
            response_control = json.loads(response[0].decode("utf-8"))
            self.assertFalse(response_control["ok"])
            self.assertEqual(response_control["error"]["type"], "validation")

            model.fail_ocr = True
            with self.assertRaises(OcrRpcRemoteError):
                client("ocr", "azur_lane", image)
            self.assertGreaterEqual(service.metrics.snapshot()["errors"], 3)
        finally:
            client.close()
            self._stop_server(stop_event, thread)

    def test_timeout_unavailable_fallback_and_concurrent_requests(self) -> None:
        image = np.zeros((4, 4, 3), dtype=np.uint8)
        model = _RpcModel()
        port, _service, stop_event, thread = self._start_server(model)
        client = _ZmqRpcClient(f"127.0.0.1:{port}", timeout=0.02)
        try:
            model.delay = 0.1
            with self.assertRaises(OcrRpcTransportError):
                client("ocr", "azur_lane", image)
            time.sleep(0.15)
            model.delay = 0
            client.close()
            client = _ZmqRpcClient(f"127.0.0.1:{port}", timeout=1)
            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(
                    executor.map(
                        lambda _: client("ocr", "azur_lane", image),
                        range(16),
                    )
                )
            self.assertEqual(results, ["ocr"] * 16)
        finally:
            client.close()
            self._stop_server(stop_event, thread)

        fallback = _FallbackModel("fallback")
        unavailable = _ZmqRpcClient(f"127.0.0.1:{_free_port()}", timeout=0.02)
        proxy = ModelProxy("azur_lane")
        proxy.client = unavailable
        proxy.online = True
        ModelProxy.online = True
        try:
            with patch.dict(
                sys.modules,
                {"module.ocr.models": self._models_module(fallback)},
            ):
                self.assertEqual(proxy.ocr(image), "fallback")
            self.assertFalse(proxy.online)
            self.assertEqual(len(fallback.calls), 1)
        finally:
            unavailable.close()

    def test_server_restart_releases_loopback_endpoint(self) -> None:
        port = _free_port()
        for marker in ("first", "second"):
            model = _RpcModel()
            stop_event = threading.Event()
            ready_event = threading.Event()
            service = _OcrRpcService(SimpleNamespace(azur_lane=model))
            server = _OcrRpcServer(port, service)
            thread = threading.Thread(
                target=server.run,
                kwargs={"stop_event": stop_event, "ready_event": ready_event},
                daemon=True,
            )
            thread.start()
            self.assertTrue(ready_event.wait(3), marker)
            client = _ZmqRpcClient(f"127.0.0.1:{port}", timeout=1)
            try:
                self.assertEqual(client.hello(), "hello")
            finally:
                client.close()
                self._stop_server(stop_event, thread)

    def test_process_lifecycle_reports_ready_and_stops_without_orphan(self) -> None:
        port = _free_port()
        client = None
        try:
            self.assertTrue(ocr_rpc.start_ocr_server_process(port))
            self.assertTrue(ocr_rpc.alive())
            client = _ZmqRpcClient(f"127.0.0.1:{port}", timeout=1)
            self.assertEqual(client.hello(), "hello")
        finally:
            if client is not None:
                client.close()
            self.assertTrue(ocr_rpc.stop_ocr_server_process())
            self.assertFalse(ocr_rpc.alive())

    def test_online_success_does_not_import_local_models(self) -> None:
        image = np.zeros((4, 4, 3), dtype=np.uint8)
        proxy = ModelProxy("azur_lane")
        proxy.online = True
        proxy.client = lambda *_args: "remote"

        original = sys.modules.pop("module.ocr.models", None)
        try:
            self.assertEqual(proxy.ocr(image), "remote")
            self.assertNotIn("module.ocr.models", sys.modules)
        finally:
            if original is not None:
                sys.modules["module.ocr.models"] = original

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

    def test_invalid_online_payload_is_not_treated_as_transport_failure(self) -> None:
        fallback = _FallbackModel("must-not-run")
        proxy = ModelProxy("azur_lane")
        proxy.online = True
        proxy.client = lambda *_args: self.fail(
            "RPC-клиент не должен получить некорректный payload"
        )

        with (
            patch.dict(
                sys.modules,
                {"module.ocr.models": self._models_module(fallback)},
            ),
            self.assertRaises(OcrRpcSecurityError),
        ):
            proxy.ocr(object())

        self.assertTrue(proxy.online)
        self.assertEqual(fallback.calls, [])

    def test_rpc_failure_switches_instance_to_local_fallback(self) -> None:
        image = np.zeros((4, 4, 3), dtype=np.uint8)
        fallback = _FallbackModel("fallback")
        proxy = ModelProxy("azur_lane")
        proxy.online = True

        def fail(*_args):
            raise RuntimeError("ошибка transport в фикстуре")

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
        self.assertGreater(MAX_RPC_BATCH_BYTES, 0)
        self.assertEqual(_validate_batch([1]), [1])
        with self.assertRaises(ValueError):
            _validate_batch([])
        with self.assertRaises(ValueError):
            _validate_batch([None] * (MAX_RPC_BATCH_IMAGES + 1))
        with self.assertRaises(ValueError):
            _validate_batch("not-a-batch")

        with (
            patch(
                "module.ocr.rpc.MAX_RPC_BATCH_BYTES",
                2,
            ),
            patch(
                "module.ocr.rpc.encode_image_payload",
                return_value=b"x",
            ),
        ):
            self.assertEqual(_encode_batch([1, 2]), [b"x", b"x"])
            with self.assertRaises(ValueError):
                _encode_batch([1, 2, 3])

        self.assertIsNone(_validate_candidate_alphabet(None))
        self.assertEqual(_validate_candidate_alphabet("ABC"), "ABC")
        with self.assertRaises(ValueError):
            _validate_candidate_alphabet(123)
        with self.assertRaises(ValueError):
            _validate_candidate_alphabet("A" * (MAX_CANDIDATE_ALPHABET_LENGTH + 1))


if __name__ == "__main__":
    unittest.main()
