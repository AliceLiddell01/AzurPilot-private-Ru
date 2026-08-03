from __future__ import annotations

from typing import Any

SCENARIO_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "model_selection": (
        "default_model_per_language", "explicit_supported_version", "auto_version",
        "unsupported_version", "fallback_to_default", "recognition_only_model",
        "detection_compatible_fallback", "unsupported_model_name", "model_alias",
        "server_specific_azur_lane_jp",
    ),
    "model_files": (
        "model_exists", "model_missing", "dictionary_missing", "detector_missing",
        "custom_ctc_missing", "ncnn_param_missing", "ncnn_bin_missing", "invalid_path",
        "closed_model",
    ),
    "onnx_runtime": (
        "cpu_session", "requested_acceleration", "provider_available", "provider_unavailable",
        "session_creation_failure", "provider_fallback", "input_names", "output_names",
        "run_success", "run_exception", "closed_session",
    ),
    "windows_ml": (
        "windows_ml_unavailable", "catalog_unavailable", "provider_absent",
        "provider_not_ready", "ensure_ready_success", "ensure_ready_failure",
        "registration_success", "registration_failure", "device_enumeration_failure",
        "qnn_npu_candidate", "openvino_npu_candidate", "openvino_gpu_candidate",
        "directml_gpu_candidate", "integrated_gpu_rejection", "discrete_gpu_acceptance",
        "software_adapter_rejection", "cpu_fallback", "vendor_ep_disabled",
        "offline_restricted_environment",
    ),
    "ncnn": (
        "package_import_success", "package_missing", "cpu_model", "vulkan_model",
        "no_vulkan_gpu", "invalid_gpu_index", "default_gpu_index", "model_load_success",
        "load_param_error", "load_model_error", "input_error", "extract_error",
        "tuple_result", "output_shape_valid", "output_shape_invalid", "model_close",
        "fp16_disabled_model",
    ),
    "rapidocr_rec_only": (
        "text_rec_output_success", "empty_txts", "empty_scores", "multiple_lines",
        "single_line", "word_results", "elapsed_time", "unsupported_image_shape",
        "gray_input", "bgr_input", "bgra_input", "float_image", "zero_sized_image",
    ),
    "detection_recognition": (
        "text_det_output_boxes", "no_boxes", "rapidocr_output",
        "boxes_txts_scores_aligned", "rotated_crop", "empty_recognized_text",
        "low_score", "multiple_boxes", "onnx_full_pipeline", "ncnn_rec_onnx_det_hybrid",
    ),
    "ocr_queue": (
        "lazy_worker_start", "single_worker_reuse", "job_success", "job_exception",
        "traceback_preservation", "reentrant_call", "queue_task_done",
        "model_release_inside_worker", "concurrent_submissions", "no_deadlock",
    ),
    "model_cache": (
        "cache_miss", "cache_hit", "different_backend", "different_device",
        "different_model_version", "vendor_ep_flag_change", "release_selected_model",
        "release_all", "close_failure", "reset",
    ),
    "ocr_classes": (
        "ocr", "ocr_yuv", "digit", "digit_yuv", "digit_counter",
        "digit_counter_yuv", "duration", "duration_yuv",
    ),
    "postprocess": (
        "i_to_1", "d_to_0", "s_to_5", "b_to_8", "valid_counter",
        "invalid_counter", "current_greater_total", "valid_duration", "invalid_duration",
        "campaign_double_hyphen", "campaign_i_1_correction", "two_digit_stage",
        "unknown_campaign_name",
    ),
    "rpc": (
        "client_connect", "hello_success", "hello_failure", "server_offline",
        "local_fallback", "remote_ocr_success", "remote_ocr_failure", "disconnect",
        "bind_success", "bind_failure", "process_start", "process_stop", "timeout",
        "serialization_boundary", "invalid_serialized_payload",
    ),
    "benchmark": (
        "archive_found", "archive_missing", "dataset_found", "dataset_missing",
        "valid_val_txt", "missing_image", "accuracy_100", "accuracy_90_99",
        "accuracy_below_90", "ocr_exception", "warm_up", "speed_loop",
        "cleanup_success", "cleanup_failure", "gpu_pass", "gpu_accuracy_failure",
        "cpu_fallback", "ncnn_no_vulkan_gpu", "macos_ane_selection",
    ),
    "false_recognition": (
        "positive_frame", "negative_similar_frame", "transition_animation_frame",
        "low_confidence_frame", "high_confidence_wrong_looking_fixture",
        "different_values", "en_global", "jp_relevant", "theme_variant",
    ),
}

RUNTIME_MATRIX_CLASS = (
    "tests.test_stage8b_runtime_scenario_matrix."
    "Stage8BRuntimeScenarioMatrixTests"
)


def runtime_fixture_test_id(category: str, scenario: str) -> str:
    return f"{RUNTIME_MATRIX_CLASS}.test_{category}__{scenario}"


def scenario_evidence() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for category, scenarios in SCENARIO_REQUIREMENTS.items():
        for scenario in scenarios:
            scenario_id = f"stage8b/{category}/{scenario}"
            fixture_test = runtime_fixture_test_id(category, scenario)
            if scenario_id in seen:
                raise ValueError(f"Duplicate Stage 8B scenario_id: {scenario_id}")
            seen.add(scenario_id)
            rows.append(
                {
                    "scenario_id": scenario_id,
                    "category": category,
                    "case_id": scenario,
                    "fixture_test": fixture_test,
                    "production_entrypoint": PRODUCTION_ENTRYPOINTS[category],
                    "expected_contract": EXPECTED_CONTRACTS[category],
                    "evidence_level": "CI_FIXTURE",
                }
            )
    return rows


PRODUCTION_ENTRYPOINTS = {
    "model_selection": "module.ocr.al_ocr._resolve_onnx_model_version",
    "model_files": "module.ocr.ncnn_ocr.NcnnRecOCR._check_model_files",
    "onnx_runtime": "module.ocr.windows_ml.create_onnx_session",
    "windows_ml": "module.ocr.windows_ml._iter_preferred_devices",
    "ncnn": "module.ocr.ncnn_ocr.NcnnRecOCR._normalize_output",
    "rapidocr_rec_only": "module.ocr.al_ocr.AlOcrCtcRecOCR._to_gray",
    "detection_recognition": "module.ocr.al_ocr.AlOcr._det_direct",
    "ocr_queue": "module.ocr.al_ocr._run_ocr_queued",
    "model_cache": "module.ocr.al_ocr._model_cache_key",
    "ocr_classes": "module.ocr.ocr.Ocr.after_process",
    "postprocess": "module.ocr.ocr.Digit.after_process",
    "rpc": "module.ocr.stage8b_rpc_security.normalize_loopback_address",
    "benchmark": "module.daemon.ocr_benchmark.OcrBenchmark._rate_speed",
    "false_recognition": "module.ocr.ocr.Duration.parse_time",
}

EXPECTED_CONTRACTS = {
    "model_selection": "model/version/default/fallback remain unchanged",
    "model_files": "missing and closed models fail without changing paths",
    "onnx_runtime": "requested, available and session providers remain distinct",
    "windows_ml": "provider priority and hardware filters remain unchanged",
    "ncnn": "CPU/Vulkan/model/output contracts remain unchanged",
    "rapidocr_rec_only": "RapidOCR output and image-shape contracts remain unchanged",
    "detection_recognition": "boxes/txts/scores correlation and order remain unchanged",
    "ocr_queue": "single worker, traceback and task_done contracts remain unchanged",
    "model_cache": "cache key and release semantics remain unchanged",
    "ocr_classes": "crop/preprocess/alphabet/result contracts remain unchanged",
    "postprocess": "corrections and domain validation remain unchanged",
    "rpc": "loopback-only trusted boundary with local fallback",
    "benchmark": "accuracy/speed/fallback decisions remain unchanged",
    "false_recognition": "diagnostics do not tune thresholds or accept invalid values",
}

BACKEND_COVERAGE = (
    {
        "backend": "ONNX Runtime CPU",
        "ci_level": "DETERMINISTIC_REFERENCE",
        "real_acceptance": "CPU_REFERENCE",
    },
    {
        "backend": "Windows ML",
        "ci_level": "SYNTHETIC_PROVIDER_FIXTURES",
        "real_acceptance": "CONFIGURED_PROVIDER_ONLY",
    },
    {
        "backend": "NCNN CPU",
        "ci_level": "CI_FIXTURE",
        "real_acceptance": "WHEN_CONFIGURED",
    },
    {
        "backend": "NCNN Vulkan",
        "ci_level": "SELECTION_FIXTURE",
        "real_acceptance": "EVIDENCE_ONLY_UNLESS_CONFIGURED",
    },
    {
        "backend": "OCR RPC",
        "ci_level": "LOOPBACK_BENIGN_FIXTURE",
        "real_acceptance": "NO_WILDCARD",
    },
)
