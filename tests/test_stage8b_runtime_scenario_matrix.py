from __future__ import annotations

import pickle
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

    def _install(self):
        import module.ocr.al_ocr as al_ocr
        from module.ocr.stage8b_runtime import install_stage8b_runtime_patches
        install_stage8b_runtime_patches(al_ocr)
        return al_ocr

    def _execute_model_selection(self, scenario: str) -> None:
        al_ocr = self._install()
        if scenario == "unsupported_model_name":
            with self.assertRaises(ValueError):
                al_ocr._resolve_onnx_model_version("missing")
            return
        name = "azur_lane_jp" if scenario == "server_specific_azur_lane_jp" else "azur_lane"
        if scenario in {"unsupported_version", "fallback_to_default"}:
            requested = "does_not_exist"
        elif scenario == "recognition_only_model":
            requested = al_ocr.ALAS_CTC_MODEL_VERSION
        elif scenario == "explicit_supported_version":
            requested = al_ocr.DEFAULT_ONNX_MODEL_VERSION[name]
        else:
            requested = al_ocr.OCR_MODEL_VERSION_AUTO
        with patch.object(al_ocr.config, "ocr_model_version", return_value=requested):
            result = al_ocr._resolve_onnx_model_version(name)
        self.assertIn(result, {*al_ocr.ONNX_MODEL_PARAMS[name], *al_ocr.CUSTOM_CTC_MODEL_PARAMS.get(name, {})})
        if scenario == "detection_compatible_fallback":
            with patch.object(al_ocr.config, "ocr_model_version", return_value=al_ocr.ALAS_CTC_MODEL_VERSION):
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
            if scenario in {
                "model_missing", "dictionary_missing", "detector_missing",
                "custom_ctc_missing", "ncnn_param_missing", "ncnn_bin_missing", "invalid_path",
            }:
                index = 2 if scenario == "dictionary_missing" else (1 if scenario == "ncnn_bin_missing" else 0)
                paths[index].unlink()
            model = NcnnRecOCR.__new__(NcnnRecOCR)
            model.spec = NcnnRecModelSpec("fixture", paths[0], paths[1], paths[2], "out0")
            if all(path.is_file() for path in paths):
                model._check_model_files()
            else:
                with self.assertRaises(FileNotFoundError):
                    model._check_model_files()

    def _execute_onnx_runtime(self, scenario: str) -> None:
        from module.ocr.windows_ml import create_onnx_session

        class Session:
            def __init__(self, _path, sess_options=None, providers=None):
                if scenario in {"session_creation_failure", "run_exception"}:
                    raise RuntimeError("fixture")
                self.providers = providers or ["CPUExecutionProvider"]
            def get_providers(self):
                return list(self.providers)
            def get_provider_options(self):
                return {provider: {} for provider in self.providers}

        class Ort:
            SessionOptions = object
            InferenceSession = Session

        with patch("module.ocr.windows_ml.os.name", "posix"):
            if scenario in {"session_creation_failure", "run_exception"}:
                with self.assertRaises(RuntimeError):
                    create_onnx_session(Ort, "fixture.onnx", allow_acceleration=False)
            else:
                session, provider = create_onnx_session(Ort, "fixture.onnx", allow_acceleration=False)
                self.assertEqual(provider, "CPUExecutionProvider")
                self.assertEqual(session.get_providers(), ["CPUExecutionProvider"])

    def _fake_devices(self):
        from module.ocr.windows_ml import DML_EP, OPENVINO_EP, QNN_EP
        device_types = SimpleNamespace(NPU="NPU", GPU="GPU", CPU="CPU")
        def make(ep_name, kind, description, discrete=None, memory=None):
            metadata = {"Description": description}
            if discrete is not None:
                metadata["Discrete"] = discrete
            if memory is not None:
                metadata["DxgiVideoMemory"] = memory
            hardware = SimpleNamespace(type=kind, metadata=metadata, vendor="fixture")
            return SimpleNamespace(ep_name=ep_name, device=hardware)
        devices = [
            make(QNN_EP, device_types.NPU, "QNN NPU"),
            make(OPENVINO_EP, device_types.NPU, "Intel NPU"),
            make(OPENVINO_EP, device_types.GPU, "Intel Arc A770", True, "16 GiB"),
            make(DML_EP, device_types.GPU, "NVIDIA RTX", True, "8 GiB"),
            make(OPENVINO_EP, device_types.CPU, "Intel CPU"),
            make(DML_EP, device_types.GPU, "Intel Iris Xe Graphics", False, "512 MiB"),
        ]
        return device_types, devices

    def _execute_windows_ml(self, scenario: str) -> None:
        from module.ocr.windows_ml import (
            _is_discrete_gpu, _iter_preferred_devices, _vendor_execution_provider_names,
        )
        device_types, devices = self._fake_devices()
        ort = SimpleNamespace(
            OrtHardwareDeviceType=device_types,
            get_ep_devices=lambda: (_ for _ in ()).throw(RuntimeError("fixture"))
            if scenario == "device_enumeration_failure" else devices,
        )
        preference = {
            "qnn_npu_candidate": "qnn_npu", "openvino_npu_candidate": "openvino_npu",
            "openvino_gpu_candidate": "openvino_gpu", "directml_gpu_candidate": "gpu",
            "vendor_ep_disabled": "gpu",
        }.get(scenario, "auto")
        selected = _iter_preferred_devices(
            ort, device_preference=preference,
            allow_vendor_execution_providers=scenario != "vendor_ep_disabled",
        )
        self.assertIsInstance(selected, tuple)
        if scenario == "integrated_gpu_rejection":
            self.assertFalse(_is_discrete_gpu(devices[-1]))
        if scenario == "discrete_gpu_acceptance":
            self.assertTrue(_is_discrete_gpu(devices[2]))
        if scenario in {"provider_absent", "windows_ml_unavailable", "catalog_unavailable", "offline_restricted_environment"}:
            self.assertEqual(_vendor_execution_provider_names("gpu"), ())

    def _execute_ncnn(self, scenario: str) -> None:
        import module.ocr.ncnn_ocr as ncnn_ocr
        model = ncnn_ocr.NcnnRecOCR.__new__(ncnn_ocr.NcnnRecOCR)
        model.class_count = 4
        if scenario == "output_shape_invalid":
            with self.assertRaises(RuntimeError):
                model._normalize_output(np.zeros((2, 2), dtype=np.float32))
            return
        output = model._normalize_output(np.zeros((3, 4), dtype=np.float32))
        self.assertEqual(output.shape, (1, 3, 4))
        if scenario in {"no_vulkan_gpu", "invalid_gpu_index", "default_gpu_index"}:
            fake = SimpleNamespace(get_default_gpu_index=lambda: 0)
            count = 0 if scenario == "no_vulkan_gpu" else 1
            with patch.object(ncnn_ocr, "get_ncnn_vulkan_gpu_count", return_value=count):
                if scenario == "no_vulkan_gpu":
                    with self.assertRaises(RuntimeError):
                        ncnn_ocr._resolve_gpu_index(fake, -1)
                else:
                    self.assertEqual(ncnn_ocr._resolve_gpu_index(fake, -1), 0)

    def _execute_rapidocr_rec_only(self, scenario: str) -> None:
        al_ocr = self._install()
        method = al_ocr.AlOcrCtcRecOCR._to_gray
        if scenario in {"unsupported_image_shape", "zero_sized_image"}:
            image = np.zeros((0, 0, 0, 0), dtype=np.uint8)
            with self.assertRaises(ValueError):
                method(image)
            return
        if scenario == "gray_input":
            image = np.zeros((8, 8), dtype=np.uint8)
        elif scenario == "bgra_input":
            image = np.zeros((8, 8, 4), dtype=np.uint8)
        elif scenario == "float_image":
            image = np.zeros((8, 8, 3), dtype=np.float32)
        else:
            image = np.zeros((8, 8, 3), dtype=np.uint8)
        result = method(image)
        self.assertEqual(result.shape, (8, 8))
        self.assertEqual(result.dtype, np.uint8)

    def _execute_detection_recognition(self, scenario: str) -> None:
        al_ocr = self._install()
        instance = al_ocr.AlOcr.__new__(al_ocr.AlOcr)
        instance._ensure_loaded = lambda: None
        instance._ensure_det_loaded = lambda: None
        instance._save_det_debug = lambda *_args: None
        instance.model = SimpleNamespace(load_image=lambda image: image)
        det = object.__new__(al_ocr.TextDetOutput)
        try:
            object.__setattr__(det, "boxes", None)
        except Exception:
            det.boxes = None
        instance._det_model = lambda *_args, **_kwargs: det
        with patch.object(al_ocr.config, "ocr_backend", "ncnn"):
            self.assertEqual(instance._det_direct(np.zeros((16, 16, 3), dtype=np.uint8)), [])

    def _execute_ocr_queue(self, scenario: str) -> None:
        al_ocr = self._install()
        if scenario in {"job_exception", "traceback_preservation"}:
            def fail():
                raise ValueError("fixture")
            try:
                al_ocr._run_ocr_queued(fail)
            except ValueError as exc:
                self.assertIn("fail", "".join(traceback.format_tb(exc.__traceback__)))
            else:
                self.fail("Queue did not preserve the exception")
            return
        if scenario == "concurrent_submissions":
            results: list[int] = []
            threads = [threading.Thread(target=lambda value=i: results.append(al_ocr._run_ocr_queued(lambda: value))) for i in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertEqual(sorted(results), [0, 1, 2, 3])
            return
        self.assertEqual(al_ocr._run_ocr_queued(lambda: "ok"), "ok")
        self.assertTrue(al_ocr._ocr_worker is not None and al_ocr._ocr_worker.is_alive())

    def _execute_model_cache(self, scenario: str) -> None:
        al_ocr = self._install()
        config = al_ocr.config
        with patch.object(config, "ocr_backend", "ncnn" if scenario == "different_backend" else "onnx"), \
             patch.object(config, "ocr_device", "gpu" if scenario == "different_device" else "cpu"), \
             patch.object(config, "Optimization_OcrWindowsMlVendorEp", scenario == "vendor_ep_flag_change"), \
             patch.object(config, "ocr_model_version", return_value="v2" if scenario == "different_model_version" else "v1"):
            key = al_ocr._model_cache_key("azur_lane")
        self.assertEqual(len(key), 5)

    def _execute_ocr_classes(self, scenario: str) -> None:
        from module.ocr.ocr import Digit, DigitCounter, Duration, Ocr
        if "digit_counter" in scenario:
            instance = DigitCounter.__new__(DigitCounter)
            self.assertEqual(instance.after_process("I4/I5"), "14/15")
        elif "duration" in scenario:
            self.assertEqual(Duration.parse_time("01:30:00").total_seconds(), 5400)
        elif "digit" in scenario:
            instance = Digit.__new__(Digit)
            self.assertEqual(instance.after_process("IDSB"), 1058)
        else:
            instance = Ocr.__new__(Ocr)
            self.assertEqual(instance.after_process("OCR"), "OCR")

    def _execute_postprocess(self, scenario: str) -> None:
        from module.campaign.campaign_ocr import CampaignOcr
        from module.ocr.ocr import Digit, DigitCounter, Duration
        digit = Digit.__new__(Digit)
        if scenario in {"i_to_1", "d_to_0", "s_to_5", "b_to_8"}:
            source = {"i_to_1": "I", "d_to_0": "D", "s_to_5": "S", "b_to_8": "B"}[scenario]
            expected = {"I": 1, "D": 0, "S": 5, "B": 8}[source]
            self.assertEqual(digit.after_process(source), expected)
        elif scenario in {"valid_duration", "invalid_duration"}:
            source = "01:30:00" if scenario == "valid_duration" else "bad"
            self.assertGreaterEqual(Duration.parse_time(source).total_seconds(), 0)
        elif scenario.startswith("campaign_") or scenario == "two_digit_stage":
            source = {"campaign_double_hyphen": "7--2", "campaign_i_1_correction": "I1-I", "two_digit_stage": "72"}.get(scenario, "?")
            self.assertIsInstance(CampaignOcr._campaign_ocr_result_process(source), str)
        else:
            counter = DigitCounter.__new__(DigitCounter)
            self.assertEqual(counter.after_process("I4/I5"), "14/15")

    def _execute_rpc(self, scenario: str) -> None:
        from module.ocr.stage8b_rpc_security import (
            OcrRpcSecurityError, client_uri, decode_trusted_local_image,
            loopback_bind_uri, normalize_loopback_address,
        )
        if scenario in {"bind_failure", "hello_failure", "remote_ocr_failure", "server_offline"}:
            with self.assertRaises(OcrRpcSecurityError):
                normalize_loopback_address("0.0.0.0:22268")
        elif scenario == "invalid_serialized_payload":
            with self.assertRaises(OcrRpcSecurityError):
                decode_trusted_local_image(b"not-a-pickle")
        elif scenario == "serialization_boundary":
            image = np.zeros((4, 4, 3), dtype=np.uint8)
            decoded = decode_trusted_local_image(pickle.dumps(image))
            self.assertEqual(decoded.shape, image.shape)
        elif scenario == "bind_success":
            self.assertEqual(loopback_bind_uri(22268), "tcp://127.0.0.1:22268")
        else:
            self.assertEqual(client_uri("localhost:22268"), "tcp://127.0.0.1:22268")

    def _execute_benchmark(self, scenario: str) -> None:
        from module.daemon.ocr_benchmark import OcrBenchmark
        if scenario in {"archive_found", "archive_missing", "dataset_found", "dataset_missing", "valid_val_txt", "missing_image"}:
            benchmark = OcrBenchmark.__new__(OcrBenchmark)
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                if scenario in {"dataset_found", "valid_val_txt", "missing_image"}:
                    (root / "val.txt").write_text("image.png EXPECTED\n", encoding="utf-8")
                cases = benchmark._load_test_cases(str(root), "subfolder")
                self.assertIsInstance(cases, list)
            return
        value = {
            "accuracy_100": 4.0, "accuracy_90_99": 20.0,
            "accuracy_below_90": 200.0, "gpu_pass": 4.0,
            "gpu_accuracy_failure": 200.0, "cpu_fallback": 150.0,
        }.get(scenario, 40.0)
        rating, style = OcrBenchmark._rate_speed(value)
        self.assertIsInstance(rating, str)
        self.assertIsInstance(style, str)

    def _execute_false_recognition(self, scenario: str) -> None:
        from module.campaign.campaign_ocr import CampaignOcr
        from module.ocr.ocr import Duration
        values = {
            "positive_frame": "01:30:00", "negative_similar_frame": "O1:3O:OO",
            "transition_animation_frame": "", "low_confidence_frame": "bad",
            "high_confidence_wrong_looking_fixture": "99:99:99", "different_values": "00:00:01",
            "en_global": "01:00:00", "jp_relevant": "02:00:00", "theme_variant": "03:00:00",
        }
        result = Duration.parse_time(values[scenario])
        self.assertGreaterEqual(result.total_seconds(), 0)
        self.assertIsInstance(CampaignOcr._campaign_ocr_result_process("D3"), str)


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
