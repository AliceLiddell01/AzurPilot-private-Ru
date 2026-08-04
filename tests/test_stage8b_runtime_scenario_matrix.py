from __future__ import annotations

import os
import tempfile
import threading
import traceback
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from dev_tools.stage8b_evidence_policy import SCENARIO_REQUIREMENTS


class Stage8BRuntimeScenarioMatrixTests(unittest.TestCase):
    maxDiff = None

    def execute_scenario(self, category: str, scenario: str) -> None:
        getattr(self, f"_execute_{category}")(scenario)

    def _execute_model_selection(self, scenario: str) -> None:
        import module.ocr.al_ocr as al_ocr

        if scenario == "unsupported_model":
            with self.assertRaises(ValueError):
                al_ocr._resolve_onnx_model_version("missing")
            return

        if scenario == "auto_default":
            requested = al_ocr.OCR_MODEL_VERSION_AUTO
            expected = al_ocr.DEFAULT_ONNX_MODEL_VERSION["azur_lane"]
            with patch.object(al_ocr.config, "ocr_model_version", return_value=requested):
                self.assertEqual(al_ocr._resolve_onnx_model_version("azur_lane"), expected)
            return

        if scenario == "explicit_supported":
            expected = "azur_lane_v6_6"
            with patch.object(al_ocr.config, "ocr_model_version", return_value=expected):
                self.assertEqual(al_ocr._resolve_onnx_model_version("azur_lane"), expected)
            return

        if scenario == "unsupported_fallback":
            expected = al_ocr.DEFAULT_ONNX_MODEL_VERSION["azur_lane"]
            with patch.object(al_ocr.config, "ocr_model_version", return_value="missing-version"):
                self.assertEqual(al_ocr._resolve_onnx_model_version("azur_lane"), expected)
            return

        with patch.object(
            al_ocr.config,
            "ocr_model_version",
            return_value=al_ocr.ALAS_CTC_MODEL_VERSION,
        ):
            params = al_ocr._get_onnx_model_params("azur_lane")
        self.assertEqual(params, al_ocr.ONNX_MODEL_PARAMS["azur_lane"]["azur_lane_v6_6"])

    def _execute_model_files(self, scenario: str) -> None:
        from module.ocr.ncnn_ocr import NcnnRecOCR, NcnnRecModelSpec

        if scenario == "closed_model":
            model = NcnnRecOCR.__new__(NcnnRecOCR)
            model.net = None
            with self.assertRaises(RuntimeError):
                model._infer(np.zeros((3, 48, 320), dtype=np.float32))
            return

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [root / "model.param", root / "model.bin", root / "dict.txt"]
            for path in paths:
                path.write_bytes(b"fixture")
            missing_index = {
                "param_missing": 0,
                "bin_missing": 1,
                "dictionary_missing": 2,
            }.get(scenario)
            if missing_index is not None:
                paths[missing_index].unlink()

            model = NcnnRecOCR.__new__(NcnnRecOCR)
            model.spec = NcnnRecModelSpec("fixture", paths[0], paths[1], paths[2], "out0")
            if missing_index is None:
                model._check_model_files()
            else:
                with self.assertRaises(FileNotFoundError) as raised:
                    model._check_model_files()
                self.assertIn(paths[missing_index].name, str(raised.exception))

    def _execute_ncnn_output(self, scenario: str) -> None:
        from module.ocr.ncnn_ocr import NcnnRecOCR

        model = NcnnRecOCR.__new__(NcnnRecOCR)
        model.class_count = 4
        if scenario == "invalid_shape":
            with self.assertRaises(RuntimeError):
                model._normalize_output(np.zeros((2, 3, 5), dtype=np.float32))
            return
        if scenario == "time_class_matrix":
            source = np.arange(12, dtype=np.float32).reshape(3, 4)
            expected = source[np.newaxis, :, :]
        elif scenario == "class_time_matrix":
            source = np.arange(12, dtype=np.float32).reshape(4, 3)
            expected = source.T[np.newaxis, :, :]
        else:
            source = np.arange(24, dtype=np.float32).reshape(2, 3, 4)
            expected = source
        actual = model._normalize_output(source)
        self.assertEqual(actual.dtype, np.float32)
        np.testing.assert_array_equal(actual, expected)

    def _execute_image_preprocess(self, scenario: str) -> None:
        from module.ocr.al_ocr import AlOcrCtcRecOCR

        if scenario == "unsupported_rank":
            with self.assertRaises(ValueError):
                AlOcrCtcRecOCR._to_gray(np.zeros((1, 2, 3, 4), dtype=np.uint8))
            return
        images = {
            "gray_uint8": np.zeros((8, 8), dtype=np.uint8),
            "bgr_uint8": np.zeros((8, 8, 3), dtype=np.uint8),
            "bgra_uint8": np.zeros((8, 8, 4), dtype=np.uint8),
            "float_unit_range": np.full((8, 8, 3), 0.5, dtype=np.float32),
        }
        result = AlOcrCtcRecOCR._to_gray(images[scenario])
        self.assertEqual(result.shape, (8, 8))
        self.assertEqual(result.dtype, np.uint8)
        if scenario == "float_unit_range":
            self.assertTrue(np.all((result >= 126) & (result <= 128)))

    def _execute_postprocess(self, scenario: str) -> None:
        from module.campaign.campaign_ocr import CampaignOcr
        from module.ocr.ocr import Digit, DigitCounter, Duration

        if scenario == "digit_corrections":
            instance = Digit.__new__(Digit)
            self.assertEqual(instance.after_process("IDSB"), 1058)
        elif scenario == "counter_corrections":
            instance = DigitCounter.__new__(DigitCounter)
            self.assertEqual(instance.after_process("I4/I5"), "14/15")
        elif scenario == "duration_valid":
            self.assertEqual(Duration.parse_time("01:30:00").total_seconds(), 5400)
        elif scenario == "duration_invalid":
            self.assertEqual(Duration.parse_time("bad").total_seconds(), 0)
        elif scenario == "campaign_double_hyphen":
            self.assertEqual(CampaignOcr._campaign_ocr_result_process("7--2"), "7-2")
        elif scenario == "campaign_i_correction":
            self.assertEqual(CampaignOcr._campaign_ocr_result_process("I1-I"), "11-1")
        else:
            self.assertEqual(CampaignOcr._campaign_ocr_result_process("72"), "7-2")

    @staticmethod
    def _rapid_output(output_type, **fields):
        output = object.__new__(output_type)
        for name, value in fields.items():
            object.__setattr__(output, name, value)
        return output

    def _execute_detection_contract(self, scenario: str) -> None:
        import module.ocr.al_ocr as al_ocr

        instance = al_ocr.AlOcr.__new__(al_ocr.AlOcr)
        instance._ensure_loaded = lambda: None
        instance._ensure_det_loaded = lambda: None
        instance._save_det_debug = lambda *_args: None

        if scenario == "ncnn_no_boxes":
            instance.model = SimpleNamespace(load_image=lambda image: image)
            det = self._rapid_output(al_ocr.TextDetOutput, boxes=None)
            instance._det_model = lambda *_args, **_kwargs: det
            with patch.object(al_ocr.config, "ocr_backend", "ncnn"):
                self.assertEqual(
                    instance._det_direct(np.zeros((16, 16, 3), dtype=np.uint8)),
                    [],
                )
            return

        boxes = np.array(
            [
                [[1, 1], [5, 1], [5, 5], [1, 5]],
                [[7, 7], [11, 7], [11, 11], [7, 11]],
            ],
            dtype=np.float32,
        )
        if scenario == "onnx_missing_text_and_scores":
            txts = None
            scores = None
            expected = [
                ("", boxes[0].tolist(), 0.0),
                ("", boxes[1].tolist(), 0.0),
            ]
        else:
            txts = ("FIRST", "SECOND")
            scores = (0.9, 0.8)
            expected = [
                ("FIRST", boxes[0].tolist(), 0.9),
                ("SECOND", boxes[1].tolist(), 0.8),
            ]
        output = self._rapid_output(
            al_ocr.RapidOCROutput,
            boxes=boxes,
            txts=txts,
            scores=scores,
        )
        instance._det_model = lambda *_args, **_kwargs: output
        with patch.object(al_ocr.config, "ocr_backend", "onnx"):
            self.assertEqual(
                instance._det_direct(np.zeros((16, 16, 3), dtype=np.uint8)),
                expected,
            )

    def _execute_queue_cache(self, scenario: str) -> None:
        import module.ocr.al_ocr as al_ocr

        if scenario == "queued_success":
            self.assertEqual(al_ocr._run_ocr_queued(lambda value: value + 1, 2), 3)
            return
        if scenario == "queued_exception_traceback":
            def fail():
                raise ValueError("fixture")

            try:
                al_ocr._run_ocr_queued(fail)
            except ValueError as exc:
                self.assertIn("fail", "".join(traceback.format_tb(exc.__traceback__)))
            else:
                self.fail("Queue did not propagate the exception")
            return
        if scenario == "reentrant_execution":
            original = al_ocr._ocr_worker_ident
            al_ocr._ocr_worker_ident = threading.get_ident()
            try:
                self.assertEqual(al_ocr._run_ocr_queued(lambda: "direct"), "direct")
            finally:
                al_ocr._ocr_worker_ident = original
            return

        device = "gpu" if scenario == "cache_key_device" else "cpu"
        version = "v2" if scenario == "cache_key_model_version" else "v1"
        with patch.object(al_ocr.config, "ocr_backend", "onnx"), \
             patch.object(al_ocr.config, "ocr_device", device), \
             patch.object(al_ocr.config, "Optimization_OcrWindowsMlVendorEp", False), \
             patch.object(al_ocr.config, "ocr_model_version", return_value=version):
            key = al_ocr._model_cache_key("azur_lane")
        self.assertEqual(key, ("azur_lane", "onnx", device, False, version))

    def _execute_windows_ml(self, scenario: str) -> None:
        from module.ocr.windows_ml import (
            _is_discrete_gpu, _iter_preferred_devices,
            _vendor_execution_provider_names, create_onnx_session,
        )

        if scenario == "cpu_session":
            class Session:
                def __init__(self, _path, sess_options=None, providers=None):
                    self.providers = providers

            ort = SimpleNamespace(SessionOptions=object, InferenceSession=Session)
            with patch("module.ocr.windows_ml.os.name", "posix"):
                session, provider = create_onnx_session(
                    ort,
                    "fixture.onnx",
                    allow_acceleration=False,
                )
            self.assertEqual(provider, "CPUExecutionProvider")
            self.assertEqual(session.providers, ["CPUExecutionProvider"])
            return
        if scenario == "vendor_provider_names":
            self.assertEqual(
                _vendor_execution_provider_names("auto"),
                ("QNNExecutionProvider", "OpenVINOExecutionProvider"),
            )
            return
        if scenario == "device_enumeration_failure":
            ort = SimpleNamespace(
                get_ep_devices=lambda: (_ for _ in ()).throw(RuntimeError("fixture")),
                OrtHardwareDeviceType=SimpleNamespace(NPU="NPU", GPU="GPU", CPU="CPU"),
            )
            self.assertEqual(_iter_preferred_devices(ort), ())
            return

        description = "Intel Iris Xe Graphics" if scenario == "integrated_gpu_rejected" else "NVIDIA RTX"
        metadata = {
            "Description": description,
            "DxgiVideoMemory": "512 MiB" if scenario == "integrated_gpu_rejected" else "8 GiB",
        }
        device = SimpleNamespace(device=SimpleNamespace(metadata=metadata))
        self.assertEqual(
            _is_discrete_gpu(device),
            scenario == "discrete_gpu_accepted",
        )

    def _execute_rpc_security(self, scenario: str) -> None:
        from module.ocr.stage8b_rpc_security import (
            OcrRpcSecurityError, decode_image_payload, encode_image_payload,
            normalize_loopback_address,
        )

        if scenario == "loopback_normalization":
            self.assertEqual(normalize_loopback_address("localhost:22268"), "127.0.0.1:22268")
            return
        if scenario == "remote_address_rejected":
            with self.assertRaises(OcrRpcSecurityError):
                normalize_loopback_address("192.0.2.1:22268")
            return
        if scenario == "object_dtype_rejected":
            with self.assertRaises(OcrRpcSecurityError):
                encode_image_payload(np.array([object()], dtype=object))
            return

        dtype = np.float32 if scenario == "float32_round_trip" else np.uint8
        image = np.arange(48, dtype=dtype).reshape(4, 4, 3)
        payload = encode_image_payload(image)
        if scenario == "truncated_payload_rejected":
            with self.assertRaises(OcrRpcSecurityError):
                decode_image_payload(payload[:-1])
            return
        decoded = decode_image_payload(payload)
        self.assertEqual(decoded.dtype, dtype)
        np.testing.assert_array_equal(decoded, image)

    def _execute_debug_privacy(self, scenario: str) -> None:
        from module.ocr.stage8b_privacy import OcrDebugOutputError, save_debug_image

        image = np.zeros((8, 8, 3), dtype=np.uint8)
        if scenario == "git_root_rejected":
            with patch.dict(os.environ, {"AZURPILOT_OCR_DEBUG": "1"}, clear=False):
                with self.assertRaises(OcrDebugOutputError):
                    save_debug_image(image, model_name="azur_lane", directory="ocr_debug")
            return

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "debug"
            enabled = "0" if scenario == "disabled_is_noop" else "1"
            with patch.dict(os.environ, {"AZURPILOT_OCR_DEBUG": enabled}, clear=False):
                if scenario == "disabled_is_noop":
                    self.assertIsNone(
                        save_debug_image(image, model_name="azur_lane", directory=target)
                    )
                    self.assertFalse(target.exists())
                elif scenario == "safe_filename":
                    path = save_debug_image(
                        image,
                        model_name="azur_lane",
                        kind="rec",
                        directory=target,
                    )
                    assert path is not None
                    self.assertRegex(path.name, r"^rec_azur_lane_\d+_[0-9a-f]{16}\.png$")
                else:
                    for _ in range(3):
                        save_debug_image(
                            image,
                            model_name="azur_lane",
                            directory=target,
                            retention=2,
                        )
                    self.assertEqual(len(list(target.glob("*.png"))), 2)

    def _execute_benchmark(self, scenario: str) -> None:
        from module.daemon.ocr_benchmark import OcrBenchmark

        value = {"fast_rating": 4.0, "medium_rating": 60.0, "slow_rating": 200.0}[scenario]
        rating, style = OcrBenchmark._rate_speed(value)
        expected_style = {
            "fast_rating": "bold bright_green",
            "medium_rating": "orange1",
            "slow_rating": "red",
        }[scenario]
        self.assertTrue(rating)
        self.assertEqual(style, expected_style)


def _make_test(category: str, scenario: str):
    def test(self: Stage8BRuntimeScenarioMatrixTests) -> None:
        self.execute_scenario(category, scenario)

    test.__name__ = f"test_{category}__{scenario}"
    test.__qualname__ = f"Stage8BRuntimeScenarioMatrixTests.{test.__name__}"
    return test


for _category, _scenarios in SCENARIO_REQUIREMENTS.items():
    for _scenario in _scenarios:
        setattr(
            Stage8BRuntimeScenarioMatrixTests,
            f"test_{_category}__{_scenario}",
            _make_test(_category, _scenario),
        )


if __name__ == "__main__":
    unittest.main()
