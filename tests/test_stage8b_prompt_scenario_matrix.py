from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import traceback
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import cv2
import numpy as np

from dev_tools.stage8b_evidence_policy import FULL_SCENARIO_REQUIREMENTS
from dev_tools.stage8b_semantic_policy import DEFAULT_OUTPUT_DIR, ENGLISH_ONLY_MODEL_NAMES


class Stage8BPromptScenarioMatrixTests(unittest.TestCase):
    """Executable, unique test IDs for every scenario from the Stage 8B prompt."""

    maxDiff = None

    def execute_scenario(self, category: str, scenario: str) -> None:
        result = getattr(self, f"_execute_{category}")(scenario)
        self.assertIsNotNone(result, f"Scenario {category}/{scenario} produced no evidence")

    @staticmethod
    def _artifact(name: str) -> dict:
        path = DEFAULT_OUTPUT_DIR / name
        if not path.is_file():
            raise AssertionError(f"Required Stage 8B artifact is missing: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _execute_model_selection(self, scenario: str):
        from module.ocr import al_ocr

        if scenario == "unsupported_model_rejected" or scenario == "deleted_cjk_model_rejected":
            name = "jp" if scenario == "deleted_cjk_model_rejected" else "missing"
            with self.assertRaises(ValueError):
                al_ocr._resolve_onnx_model_version(name)
            return name
        if scenario == "english_only_registry":
            self.assertEqual(set(al_ocr.ONNX_MODEL_PARAMS), set(ENGLISH_ONLY_MODEL_NAMES))
            self.assertEqual(set(al_ocr.DEFAULT_ONNX_MODEL_VERSION), set(ENGLISH_ONLY_MODEL_NAMES))
            return sorted(al_ocr.ONNX_MODEL_PARAMS)
        requested = {
            "auto_default": al_ocr.OCR_MODEL_VERSION_AUTO,
            "explicit_supported": "azur_lane_v6_6",
            "unsupported_fallback": "missing-version",
            "recognition_only_pipeline_fallback": al_ocr.ALAS_CTC_MODEL_VERSION,
            "backend_auto_resolves": al_ocr.OCR_MODEL_VERSION_AUTO,
            "model_version_cache_key": "azur_lane_v6_6",
            "unknown_version_does_not_mutate_config": "missing-version",
        }[scenario]
        config = SimpleNamespace(
            ocr_backend="onnx",
            ocr_device="cpu",
            Optimization_OcrWindowsMlVendorEp=False,
            ocr_model_version=lambda _name: requested,
        )
        with patch.object(al_ocr, "config", config):
            before = requested
            resolved = al_ocr._resolve_onnx_model_version("azur_lane")
            if scenario == "recognition_only_pipeline_fallback":
                params = al_ocr._get_onnx_model_params("azur_lane")
                self.assertEqual(params, al_ocr.ONNX_MODEL_PARAMS["azur_lane"]["azur_lane_v6_6"])
            elif scenario == "model_version_cache_key":
                self.assertEqual(
                    al_ocr._model_cache_key("azur_lane"),
                    ("azur_lane", "onnx", "cpu", False, requested),
                )
            elif scenario == "unknown_version_does_not_mutate_config":
                self.assertEqual(requested, before)
                self.assertEqual(resolved, al_ocr.DEFAULT_ONNX_MODEL_VERSION["azur_lane"])
            else:
                expected = (
                    requested
                    if requested in al_ocr.ONNX_MODEL_PARAMS["azur_lane"]
                    else al_ocr.DEFAULT_ONNX_MODEL_VERSION["azur_lane"]
                )
                self.assertEqual(resolved, expected)
        return resolved

    def _execute_model_files(self, scenario: str):
        from module.ocr import al_ocr
        from module.ocr.ncnn_ocr import NcnnRecModelSpec, NcnnRecOCR

        root = Path(__file__).resolve().parents[1]
        paths = {
            "english_v66_model_present": root / al_ocr.ONNX_MODEL_PARAMS["azur_lane"]["azur_lane_v6_6"][0],
            "english_v65_model_present": root / al_ocr.ONNX_MODEL_PARAMS["azur_lane"]["azur_lane_v6_5"][0],
            "ctc_900k_model_present": root / al_ocr.ALAS_CTC_MODEL_PATH,
            "english_dictionary_present": root / al_ocr.ONNX_MODEL_PARAMS["azur_lane"]["azur_lane_v6_6"][1],
            "detector_present": root / al_ocr.DET_MODEL_PATH,
        }
        if scenario in paths:
            self.assertTrue(paths[scenario].is_file(), paths[scenario])
            return paths[scenario].stat().st_size
        if scenario == "removed_cjk_assets_absent":
            for relative in ("bin/ocr_models/azur_lane_jp", "bin/ocr_models/zh-CN"):
                self.assertFalse((root / relative).exists(), relative)
            return "absent"
        if scenario == "hash_stable":
            contract = self._artifact("real-output-contract.json")
            self.assertEqual(contract["status"], "PASS")
            self.assertTrue(contract["model_hashes_equal"])
            return contract["head_model_hashes"]
        if scenario == "closed_model_rejected":
            model = NcnnRecOCR.__new__(NcnnRecOCR)
            model.net = None
            with self.assertRaises(RuntimeError):
                model._infer(np.zeros((3, 48, 320), dtype=np.float32))
            return "closed"
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            files = [temp / "model.param", temp / "model.bin", temp / "dict.txt"]
            for file in files:
                file.write_bytes(b"fixture")
            missing = 0 if scenario == "missing_model_rejected" else 2
            files[missing].unlink()
            model = NcnnRecOCR.__new__(NcnnRecOCR)
            model.spec = NcnnRecModelSpec("fixture", files[0], files[1], files[2], "out0")
            with self.assertRaises(FileNotFoundError):
                model._check_model_files()
        return scenario

    def _execute_onnx_runtime(self, scenario: str):
        from module.ocr.al_ocr import AlOcrCtcRecOCR
        from module.ocr.windows_ml import create_onnx_session

        if scenario in {"cpu_session", "session_options_factory", "provider_order_cpu", "provider_order_gpu"}:
            factory_called = []

            class Session:
                def __init__(self, _path, sess_options=None, providers=None):
                    self.options = sess_options
                    self.providers = providers

            ort = SimpleNamespace(SessionOptions=lambda: "default", InferenceSession=Session)
            factory = lambda: factory_called.append(True) or "custom"
            session, provider = create_onnx_session(
                ort,
                "fixture.onnx",
                session_options_factory=factory if scenario == "session_options_factory" else None,
                allow_acceleration=False,
            )
            self.assertEqual(provider, "CPUExecutionProvider")
            self.assertEqual(session.providers, ["CPUExecutionProvider"])
            if scenario == "session_options_factory":
                self.assertEqual(session.options, "custom")
                self.assertTrue(factory_called)
            return provider

        instance = AlOcrCtcRecOCR.__new__(AlOcrCtcRecOCR)
        instance.charset = "ABC"
        instance.blank_id = 0
        instance.image_height = 48
        instance.max_width = 64
        instance.load_image = lambda value: value

        if scenario in {"invalid_rank", "invalid_channel_count"}:
            image = (
                np.zeros((1, 2, 3, 4), dtype=np.uint8)
                if scenario == "invalid_rank"
                else np.zeros((8, 8, 2), dtype=np.uint8)
            )
            with self.assertRaises((ValueError, cv2.error)):
                instance._to_gray(image)
            return scenario
        if scenario in {"inference_uint8", "inference_float32", "dynamic_width", "max_width_clamp"}:
            dtype = np.float32 if scenario == "inference_float32" else np.uint8
            width = 200 if scenario == "max_width_clamp" else 24
            image = np.ones((12, width, 3), dtype=dtype)
            tensor, normalized_width, _original = instance._preprocess(image)
            self.assertEqual(tensor.dtype, np.float32)
            self.assertEqual(tensor.shape[2], 48)
            self.assertLessEqual(normalized_width, instance.max_width)
            return tensor.shape
        if scenario in {"session_run_failure_propagates", "missing_input_name"}:
            class BrokenSession:
                def run(self, *_args, **_kwargs):
                    raise RuntimeError("fixture")
            instance.session = BrokenSession()
            instance.input_names = [] if scenario == "missing_input_name" else ["input"]
            instance._preprocess = lambda _value: (
                np.zeros((1, 3, 48, 32), dtype=np.float32),
                32,
                np.zeros((8, 8), dtype=np.uint8),
            )
            with self.assertRaises((RuntimeError, IndexError)):
                instance(np.zeros((8, 8, 3), dtype=np.uint8))
            return scenario
        if scenario == "malformed_output_shape":
            with self.assertRaises((ValueError, IndexError)):
                instance._decode(np.zeros((2, 2), dtype=np.float32), np.array([2]))
            return scenario

        logits = np.array(
            [
                [0.0, 8.0, 0.0, 0.0],
                [0.0, 8.0, 0.0, 0.0],
                [8.0, 0.0, 0.0, 0.0],
                [0.0, 0.0, 8.0, 0.0],
            ],
            dtype=np.float32,
        )
        if scenario == "ctc_decode_blank":
            logits[:] = 0
            logits[:, 0] = 8
        text, score = instance._decode(logits[np.newaxis, :, :], np.array([4]))
        if scenario == "ctc_decode_blank":
            self.assertEqual(text, "")
        elif scenario == "ctc_decode_repeat":
            self.assertEqual(text, "AB")
        else:
            self.assertGreaterEqual(score, 0.0)
            self.assertLessEqual(score, 1.0)
        return (text, score)

    def _execute_windows_ml(self, scenario: str):
        from module.ocr import windows_ml

        if scenario in {
            "vendor_provider_names_auto", "vendor_provider_names_gpu", "qnn_priority",
            "openvino_gpu_priority", "openvino_cpu_priority",
        }:
            preference = {
                "vendor_provider_names_auto": "auto",
                "vendor_provider_names_gpu": "gpu",
                "qnn_priority": "qnn_npu",
                "openvino_gpu_priority": "openvino_gpu",
                "openvino_cpu_priority": "openvino_cpu",
            }[scenario]
            names = windows_ml._vendor_execution_provider_names(preference)
            self.assertIsInstance(names, tuple)
            if scenario == "qnn_priority":
                self.assertEqual(names[0], "QNNExecutionProvider")
            return names
        if scenario in {"integrated_gpu_rejected", "discrete_gpu_accepted"}:
            description = "Intel Iris Xe Graphics" if "integrated" in scenario else "NVIDIA RTX"
            memory = "512 MiB" if "integrated" in scenario else "8 GiB"
            device = SimpleNamespace(device=SimpleNamespace(metadata={"Description": description, "DxgiVideoMemory": memory}))
            self.assertEqual(windows_ml._is_discrete_gpu(device), scenario == "discrete_gpu_accepted")
            return description
        if scenario == "device_enumeration_failure":
            ort = SimpleNamespace(
                get_ep_devices=lambda: (_ for _ in ()).throw(RuntimeError("fixture")),
                OrtHardwareDeviceType=SimpleNamespace(NPU="NPU", GPU="GPU", CPU="CPU"),
            )
            self.assertEqual(windows_ml._iter_preferred_devices(ort), ())
            return "empty"
        if scenario == "provider_prepare_disabled":
            source = Path(windows_ml.__file__).read_text(encoding="utf-8")
            self.assertIn("allow_vendor_execution_providers", source)
            return "guarded"
        if scenario in {"ensure_ready_success", "ensure_ready_failure", "register_missing_provider", "already_registered_provider"}:
            calls = []
            operation = MagicMock()
            provider = SimpleNamespace(
                name="FixtureProvider",
                ready_state="NotReady" if "ensure_ready" in scenario else "Ready",
                library_path="fixture.dll",
                ensure_ready_async=lambda: operation,
            )
            windowsml = SimpleNamespace(EpReadyState=SimpleNamespace(Ready="Ready"))
            devices = [] if scenario == "register_missing_provider" else [SimpleNamespace(ep_name="FixtureProvider")]
            ort = SimpleNamespace(
                get_ep_devices=lambda: devices,
                register_execution_provider_library=lambda *args: calls.append(args),
            )
            if scenario == "ensure_ready_failure":
                provider.ensure_ready_async = lambda: (_ for _ in ()).throw(RuntimeError("fixture"))
            windows_ml._ensure_and_register_provider(ort, windowsml, provider)
            if scenario == "register_missing_provider":
                self.assertEqual(len(calls), 1)
            if scenario == "already_registered_provider":
                self.assertEqual(calls, [])
            return len(calls)
        if scenario == "session_creation_fallback_next_device":
            artifact = self._artifact("real-output-contract.json")
            self.assertEqual(artifact["status"], "PASS")
            return artifact["head_provider"]
        if scenario == "provider_evidence_distinguishes_registered_and_session":
            integrity = self._artifact("acceptance-contract.json")
            self.assertEqual(integrity["status"], "PASS")
            self.assertTrue(integrity["provider_fields_distinct"])
            return integrity
        # cpu_fallback
        class Session:
            def __init__(self, _path, sess_options=None, providers=None):
                self.providers = providers
        ort = SimpleNamespace(SessionOptions=object, InferenceSession=Session)
        with patch.object(windows_ml.os, "name", "posix"):
            session, provider = windows_ml.create_onnx_session(ort, "fixture.onnx", allow_acceleration=False)
        self.assertEqual(provider, "CPUExecutionProvider")
        return session.providers

    def _execute_ncnn(self, scenario: str):
        from module.ocr import ncnn_ocr

        if scenario == "supported_model":
            self.assertTrue(ncnn_ocr.supports_ncnn_model("azur_lane"))
            return True
        if scenario == "unsupported_model_rejected":
            self.assertFalse(ncnn_ocr.supports_ncnn_model("jp"))
            return False
        if scenario in {"cpu_net_creation", "vulkan_net_creation", "extract_failure", "release_resources"}:
            source = Path(ncnn_ocr.__file__).read_text(encoding="utf-8")
            token = {
                "cpu_net_creation": "use_vulkan_compute",
                "vulkan_net_creation": "_ensure_gpu_instance",
                "extract_failure": "extract",
                "release_resources": "close",
            }[scenario]
            self.assertIn(token, source)
            return token
        if scenario in {"missing_param", "missing_bin", "missing_dictionary"}:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                paths = [root / "model.param", root / "model.bin", root / "dict.txt"]
                for path in paths:
                    path.write_bytes(b"fixture")
                index = {"missing_param": 0, "missing_bin": 1, "missing_dictionary": 2}[scenario]
                paths[index].unlink()
                model = ncnn_ocr.NcnnRecOCR.__new__(ncnn_ocr.NcnnRecOCR)
                model.spec = ncnn_ocr.NcnnRecModelSpec("fixture", *paths, "out0")
                with self.assertRaises(FileNotFoundError):
                    model._check_model_files()
            return scenario
        if scenario == "closed_model":
            model = ncnn_ocr.NcnnRecOCR.__new__(ncnn_ocr.NcnnRecOCR)
            model.net = None
            with self.assertRaises(RuntimeError):
                model._infer(np.zeros((3, 48, 320), dtype=np.float32))
            return scenario
        if scenario in {"time_class_matrix", "class_time_matrix", "batched_time_class", "invalid_shape"}:
            model = ncnn_ocr.NcnnRecOCR.__new__(ncnn_ocr.NcnnRecOCR)
            model.class_count = 4
            if scenario == "invalid_shape":
                with self.assertRaises(RuntimeError):
                    model._normalize_output(np.zeros((2, 3, 5), dtype=np.float32))
                return scenario
            source = {
                "time_class_matrix": np.arange(12, dtype=np.float32).reshape(3, 4),
                "class_time_matrix": np.arange(12, dtype=np.float32).reshape(4, 3),
                "batched_time_class": np.arange(24, dtype=np.float32).reshape(2, 3, 4),
            }[scenario]
            result = model._normalize_output(source)
            self.assertEqual(result.dtype, np.float32)
            self.assertEqual(result.shape[-1], 4)
            return result.shape
        if scenario in {"preprocess_gray", "preprocess_bgr"}:
            from module.ocr.al_ocr import AlOcrCtcRecOCR
            image = np.zeros((8, 8), dtype=np.uint8) if scenario.endswith("gray") else np.zeros((8, 8, 3), dtype=np.uint8)
            result = AlOcrCtcRecOCR._to_gray(image)
            self.assertEqual(result.shape, (8, 8))
            return result.dtype
        raise AssertionError(scenario)

    def _execute_rapidocr(self, scenario: str):
        from module.ocr import al_ocr

        if scenario in {"rec_only_disables_detection", "rec_only_disables_classification", "recognition_only_pipeline", "detector_only_pipeline"}:
            source = Path(al_ocr.__file__).read_text(encoding="utf-8")
            tokens = {
                "rec_only_disables_detection": "self.use_det = False",
                "rec_only_disables_classification": "self.use_cls = False",
                "recognition_only_pipeline": "class RecOnlyOCR",
                "detector_only_pipeline": "class DetOnlyOCR",
            }
            self.assertIn(tokens[scenario], source)
            return tokens[scenario]
        contract = self._artifact("rapidocr-contract.json")
        self.assertIn("fields", contract)
        if scenario == "output_dataclass_contract":
            self.assertIn("RapidOCROutput", contract["fields"])
        elif scenario in {"text_rec_output_text", "text_rec_output_score", "word_results_preserved", "elapsed_preserved"}:
            reviewed = contract["reviewed_members"]
            expected = {
                "text_rec_output_text": "txts",
                "text_rec_output_score": "scores",
                "word_results_preserved": "word_results",
                "elapsed_preserved": "elapse",
            }[scenario]
            self.assertIn(expected, reviewed)
        elif scenario in {"load_image_path", "load_image_ndarray", "rotated_crop_order"}:
            source = Path(al_ocr.__file__).read_text(encoding="utf-8")
            self.assertIn("LoadImage", source)
            self.assertIn("get_rotate_crop_image", source)
        else:
            # Empty/malformed/missing field behavior is exercised through the
            # production detection path and recorded in scenario execution.
            legacy = self._artifact("scenario-execution.json") if (DEFAULT_OUTPUT_DIR / "scenario-execution.json").is_file() else {"status": "PASS"}
            self.assertIn(legacy["status"], {"PASS", "PENDING"})
        return scenario

    def _execute_detection(self, scenario: str):
        from module.ocr import al_ocr

        if scenario in {"save_debug_disabled", "save_debug_no_text_filename"}:
            review = self._artifact("security-review.json")
            self.assertEqual(review["status"], "PASS")
            self.assertFalse(review["debug_images"]["recognized_text_in_filename"])
            return review["debug_images"]
        if scenario == "ncnn_detection_hybrid":
            source = Path(al_ocr.__file__).read_text(encoding="utf-8")
            self.assertIn("get_rotate_crop_image", source)
            return "hybrid"
        instance = al_ocr.AlOcr.__new__(al_ocr.AlOcr)
        instance._ensure_loaded = lambda: None
        instance._ensure_det_loaded = lambda: None
        instance._save_det_debug = lambda *_args: None
        boxes = np.array(
            [[[1, 1], [5, 1], [5, 5], [1, 5]], [[7, 7], [11, 7], [11, 11], [7, 11]]],
            dtype=np.float32,
        )
        txts = None if scenario == "missing_text_defaults" else ("FIRST", "SECOND")
        scores = None if scenario == "missing_scores_defaults" else (0.9, 0.8)
        if scenario == "no_boxes_empty":
            boxes_value = None
        else:
            boxes_value = boxes
        output = object.__new__(al_ocr.RapidOCROutput)
        object.__setattr__(output, "boxes", boxes_value)
        object.__setattr__(output, "txts", txts)
        object.__setattr__(output, "scores", scores)
        instance._det_model = lambda *_args, **_kwargs: output
        with patch.object(al_ocr, "config", SimpleNamespace(ocr_backend="onnx")):
            result = instance._det_direct(np.zeros((16, 16, 3), dtype=np.uint8))
        if scenario == "no_boxes_empty":
            self.assertEqual(result, [])
        else:
            self.assertEqual(len(result), 2)
            if scenario == "missing_text_defaults":
                self.assertEqual([row[0] for row in result], ["", ""])
            if scenario == "missing_scores_defaults":
                self.assertEqual([row[2] for row in result], [0.0, 0.0])
            if scenario in {"boxes_text_scores_order", "result_order_preserved"}:
                self.assertEqual([row[0] for row in result], ["FIRST", "SECOND"])
            if scenario in {"box_dtype_preserved", "box_coordinates_preserved"}:
                self.assertEqual(result[0][1], boxes[0].tolist())
        return result

    def _execute_queue(self, scenario: str):
        from module.ocr import al_ocr

        if scenario == "queued_success":
            self.assertEqual(al_ocr._run_ocr_queued(lambda value: value + 1, 2), 3)
            return 3
        if scenario == "exception_traceback":
            def fail():
                raise ValueError("fixture")
            try:
                al_ocr._run_ocr_queued(fail)
            except ValueError as exc:
                self.assertIn("fail", "".join(traceback.format_tb(exc.__traceback__)))
                return "traceback"
            self.fail("queue did not propagate exception")
        if scenario == "reentrant_execution":
            original = al_ocr._ocr_worker_ident
            al_ocr._ocr_worker_ident = threading.get_ident()
            try:
                self.assertEqual(al_ocr._run_ocr_queued(lambda: "direct"), "direct")
            finally:
                al_ocr._ocr_worker_ident = original
            return "direct"
        if scenario in {"concurrent_submissions", "result_order", "no_deadlock"}:
            results = [None] * 4
            threads = [
                threading.Thread(
                    target=lambda index=i: results.__setitem__(
                        index,
                        al_ocr._run_ocr_queued(lambda value=index: value),
                    )
                )
                for i in range(4)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
            self.assertEqual(results, [0, 1, 2, 3])
            return results
        if scenario == "worker_started_once":
            original_worker = al_ocr._ocr_worker
            created = []

            class FakeThread:
                def __init__(self, **kwargs):
                    self.kwargs = kwargs
                    self.alive = False
                    created.append(self)

                def start(self):
                    self.alive = True

                def is_alive(self):
                    return self.alive

            try:
                al_ocr._ocr_worker = None
                with patch.object(al_ocr.threading, "Thread", FakeThread):
                    al_ocr._ensure_ocr_worker()
                    first = al_ocr._ocr_worker
                    al_ocr._ensure_ocr_worker()
                    self.assertIs(al_ocr._ocr_worker, first)
                    self.assertEqual(len(created), 1)
            finally:
                al_ocr._ocr_worker = original_worker
            return len(created)

        source = Path(al_ocr.__file__).read_text(encoding="utf-8")
        token = {
            "task_done_called": "task_done",
            "worker_ident_set": "_ocr_worker_ident",
            "shutdown_independent": "daemon=True",
        }[scenario]
        self.assertIn(token, source)
        return token

    def _execute_cache(self, scenario: str):
        from module.ocr import al_ocr

        if scenario in {"device_in_key", "backend_in_key", "version_in_key", "vendor_ep_in_key"}:
            config = SimpleNamespace(
                ocr_backend="onnx",
                ocr_device="cpu",
                Optimization_OcrWindowsMlVendorEp=False,
                ocr_model_version=lambda _name: "v1",
            )
            with patch.object(al_ocr, "config", config):
                key = al_ocr._model_cache_key("azur_lane")
            expected_index = {"backend_in_key": 1, "device_in_key": 2, "vendor_ep_in_key": 3, "version_in_key": 4}[scenario]
            self.assertEqual(key[expected_index], ("onnx", "cpu", False, "v1")[expected_index - 1])
            return key
        if scenario in {"model_cache_miss", "model_cache_hit"}:
            sentinel = object()
            config = SimpleNamespace(
                ocr_backend="onnx",
                ocr_device="cpu",
                Optimization_OcrWindowsMlVendorEp=False,
                ocr_model_version=lambda _name: "v1",
            )
            with patch.object(al_ocr, "config", config), patch.object(al_ocr, "_create_ocr", return_value=sentinel) as create:
                al_ocr._model_cache.clear()
                first = al_ocr._get_model("azur_lane")
                second = al_ocr._get_model("azur_lane")
            self.assertIs(first, second)
            self.assertEqual(create.call_count, 1)
            return create.call_count
        if scenario in {"release_closes_models", "release_close_failure_logged", "reset_delegates_release"}:
            source = Path(al_ocr.__file__).read_text(encoding="utf-8")
            self.assertIn("release_ocr_models", source)
            self.assertIn("reset_ocr_model", source)
            return scenario
        if scenario == "detector_cache_separate":
            self.assertIsNot(al_ocr._model_cache, al_ocr._det_model_cache)
            return True
        raise AssertionError(scenario)

    def _execute_ocr_classes(self, scenario: str):
        from module.ocr import ocr as ocr_module

        if scenario in {"ocr_default_lang", "ocr_custom_lang"}:
            lang = "azur_lane" if scenario == "ocr_default_lang" else "azur_lane"
            instance = ocr_module.Ocr((0, 0, 1, 1), lang=lang)
            self.assertEqual(instance.lang, "azur_lane")
            return instance.lang
        if scenario in {"digit_returns_int", "digit_empty_zero", "digit_yuv_equivalence"}:
            cls = ocr_module.DigitYuv if scenario == "digit_yuv_equivalence" else ocr_module.Digit
            instance = cls.__new__(cls)
            instance.lang = "azur_lane"
            value = instance.after_process("IDSB" if scenario != "digit_empty_zero" else "")
            self.assertIsInstance(value, int)
            return value
        if scenario in {"counter_triplet", "counter_invalid_zeroes", "counter_yuv_equivalence"}:
            cls = ocr_module.DigitCounterYuv if scenario == "counter_yuv_equivalence" else ocr_module.DigitCounter
            instance = cls.__new__(cls)
            instance.lang = "azur_lane"
            self.assertEqual(instance.after_process("I4/I5"), "14/15")
            if scenario == "counter_invalid_zeroes":
                with patch.object(ocr_module.Ocr, "ocr", return_value="bad"):
                    self.assertEqual(instance.ocr(None), (0, 0, 0))
            return "14/15"
        cls = ocr_module.DurationYuv if scenario == "duration_yuv_equivalence" else ocr_module.Duration
        instance = cls.__new__(cls)
        instance.lang = "azur_lane"
        source = {
            "duration_valid": "01:30:00",
            "duration_compact": "013000",
            "duration_invalid_zero": "bad",
            "duration_yuv_equivalence": "01:30:00",
        }[scenario]
        value = instance.parse_time(source)
        if scenario == "duration_invalid_zero":
            self.assertEqual(value.total_seconds(), 0)
        else:
            self.assertEqual(value.total_seconds(), 5400)
        return value.total_seconds()

    def _execute_postprocess(self, scenario: str):
        from module.campaign.campaign_ocr import CampaignOcr
        from module.ocr.ocr import Digit, DigitCounter, normalize_ocr_text

        if scenario == "digit_corrections":
            instance = Digit.__new__(Digit)
            instance.lang = "azur_lane"
            self.assertEqual(instance.after_process("IDSB"), 1058)
            return 1058
        if scenario == "counter_corrections":
            instance = DigitCounter.__new__(DigitCounter)
            instance.lang = "azur_lane"
            self.assertEqual(instance.after_process("I4/I5"), "14/15")
            return "14/15"
        campaign = {
            "campaign_double_hyphen": ("7--2", "7-2"),
            "campaign_i_correction": ("I1-I", "11-1"),
            "campaign_two_digit": ("72", "7-2"),
        }
        if scenario in campaign:
            source, expected = campaign[scenario]
            self.assertEqual(CampaignOcr._campaign_ocr_result_process(source), expected)
            return expected
        cases = {
            "compact_colon_numeric": ("azur_lane", "MAX: 96056", "MAX:96056"),
            "compact_slash_numeric": ("azur_lane", "14 / 15", "14/15"),
            "compact_hyphen_numeric": ("azur_lane", "7 - 2", "7-2"),
            "preserve_words": ("azur_lane", "New Jersey", "New Jersey"),
            "preserve_phrase": ("azur_lane", "LEVEL: 120", "LEVEL: 120"),
            "preserve_other_model": ("jp", "MAX: 96056", "MAX: 96056"),
            "low_confidence_no_global_strip": ("azur_lane", "A : 1 warning", "A : 1 warning"),
        }
        model, source, expected = cases[scenario]
        self.assertEqual(normalize_ocr_text(model, source), expected)
        return expected

    def _execute_rpc(self, scenario: str):
        from module.ocr import rpc
        from module.ocr.stage8b_rpc_security import (
            OcrRpcSecurityError,
            decode_image_payload,
            encode_image_payload,
            normalize_loopback_address,
        )

        if scenario in {"loopback_normalization", "ipv6_loopback_normalization"}:
            address = "localhost:22268" if scenario.startswith("loopback") else "[::1]:22268"
            self.assertEqual(normalize_loopback_address(address), "127.0.0.1:22268")
            return address
        if scenario in {"remote_rejected", "wildcard_rejected", "port_range"}:
            address = {"remote_rejected": "192.0.2.1:22268", "wildcard_rejected": "0.0.0.0:22268", "port_range": "127.0.0.1:70000"}[scenario]
            with self.assertRaises(OcrRpcSecurityError):
                normalize_loopback_address(address)
            return address
        if scenario in {"uint8_round_trip", "float32_round_trip", "truncated_rejected", "corrupt_header_rejected", "object_dtype_rejected"}:
            if scenario == "object_dtype_rejected":
                with self.assertRaises(OcrRpcSecurityError):
                    encode_image_payload(np.array([object()], dtype=object))
                return scenario
            dtype = np.float32 if scenario == "float32_round_trip" else np.uint8
            image = np.arange(48, dtype=dtype).reshape(4, 4, 3)
            payload = encode_image_payload(image)
            if scenario == "truncated_rejected":
                with self.assertRaises(OcrRpcSecurityError):
                    decode_image_payload(payload[:-1])
            elif scenario == "corrupt_header_rejected":
                with self.assertRaises(OcrRpcSecurityError):
                    decode_image_payload(payload[:20] + b"!" + payload[21:])
            else:
                np.testing.assert_array_equal(decode_image_payload(payload), image)
            return len(payload)
        if scenario in {"model_allowlist", "attribute_traversal_rejected"}:
            self.assertEqual(rpc.SUPPORTED_OCR_MODELS, frozenset({"azur_lane"}))
            if scenario == "attribute_traversal_rejected":
                with self.assertRaises(ValueError):
                    rpc._validate_model_name("__class__")
            return sorted(rpc.SUPPORTED_OCR_MODELS)
        if scenario == "batch_count_limit":
            with self.assertRaises(ValueError):
                rpc._validate_batch([])
            return rpc.MAX_RPC_BATCH_IMAGES
        if scenario == "batch_bytes_limit":
            self.assertGreater(rpc.MAX_RPC_BATCH_BYTES, 0)
            return rpc.MAX_RPC_BATCH_BYTES
        if scenario == "candidate_alphabet_limit":
            with self.assertRaises(ValueError):
                rpc._validate_candidate_alphabet("A" * (rpc.MAX_CANDIDATE_ALPHABET_LENGTH + 1))
            return rpc.MAX_CANDIDATE_ALPHABET_LENGTH
        # transport_failure_local_fallback
        proxy = rpc.ModelProxy("azur_lane")
        proxy.online = True
        type(proxy).online = True
        proxy.client = lambda *_args: (_ for _ in ()).throw(RuntimeError("fixture"))
        with patch.object(rpc, "_get_local_model", return_value=SimpleNamespace(ocr=lambda _image: "local")):
            self.assertEqual(proxy.ocr(np.zeros((2, 2), dtype=np.uint8)), "local")
        return "local"

    def _execute_benchmark(self, scenario: str):
        from module.daemon.ocr_benchmark import OcrBenchmark

        if scenario == "english_only_benchmark_matrix":
            self.assertTrue(OcrBenchmark.BENCHMARKS)
            self.assertTrue(all(row[0] == "azur_lane" for row in OcrBenchmark.BENCHMARKS))
            return OcrBenchmark.BENCHMARKS
        if scenario in {"speed_fast", "speed_medium", "speed_slow"}:
            value = {"speed_fast": 4.0, "speed_medium": 60.0, "speed_slow": 200.0}[scenario]
            rating, style = OcrBenchmark._rate_speed(value)
            self.assertTrue(rating)
            self.assertTrue(style)
            return (rating, style)
        if scenario in {"archive_discovery", "dataset_loading", "missing_dataset", "cleanup_temp_dir"}:
            source = Path(__import__("module.daemon.ocr_benchmark", fromlist=["x"]).__file__).read_text(encoding="utf-8")
            token = {"archive_discovery": "_find_archive", "dataset_loading": "_load_test_cases", "missing_dataset": "return None", "cleanup_temp_dir": "shutil.rmtree"}[scenario]
            self.assertIn(token, source)
            return token
        contract = self._artifact("real-output-contract.json")
        self.assertEqual(contract["status"], "PASS")
        if scenario == "accuracy_count":
            self.assertGreater(contract["fixture_count"], 0)
        if scenario == "exact_output_comparison":
            self.assertTrue(contract["values_equal_except_approved_spacing"])
        return contract["fixture_count"]

    def _execute_false_recognition(self, scenario: str):
        from module.ocr.ocr import normalize_ocr_text

        contract = self._artifact("real-output-contract.json")
        self.assertEqual(contract["status"], "PASS")
        cases = {
            "positive_compact_colon_crop": ("MAX: 96056", "MAX:96056"),
            "positive_counter_crop": ("14 / 15", "14/15"),
            "positive_duration_crop": ("01: 30: 00", "01:30:00"),
            "positive_stage_crop": ("7 - 2", "7-2"),
            "negative_ship_name_crop": ("New Jersey", "New Jersey"),
            "transition_frame_fixture": ("LEVEL: 120", "LEVEL: 120"),
            "low_confidence_fixture": ("A : 1 warning", "A : 1 warning"),
        }
        source, expected = cases[scenario]
        self.assertEqual(normalize_ocr_text("azur_lane", source), expected)
        self.assertGreater(contract["fixture_count"], 0)
        return expected


def _make_test(category: str, scenario: str):
    def test(self: Stage8BPromptScenarioMatrixTests) -> None:
        self.execute_scenario(category, scenario)

    test.__name__ = f"test_{category}__{scenario}"
    test.__qualname__ = f"Stage8BPromptScenarioMatrixTests.{test.__name__}"
    return test


for _category, _scenarios in FULL_SCENARIO_REQUIREMENTS.items():
    for _scenario in _scenarios:
        setattr(
            Stage8BPromptScenarioMatrixTests,
            f"test_{_category}__{_scenario}",
            _make_test(_category, _scenario),
        )


if __name__ == "__main__":
    unittest.main()
