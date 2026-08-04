from __future__ import annotations

from typing import Any

SCENARIO_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "model_selection": (
        "auto_default", "explicit_supported", "unsupported_fallback", "unsupported_model",
        "recognition_only_pipeline_fallback",
    ),
    "model_files": (
        "all_files_present", "param_missing", "bin_missing", "dictionary_missing",
        "closed_model",
    ),
    "ncnn_output": (
        "time_class_matrix", "class_time_matrix", "batched_time_class", "invalid_shape",
    ),
    "image_preprocess": (
        "gray_uint8", "bgr_uint8", "bgra_uint8", "float_unit_range", "unsupported_rank",
    ),
    "postprocess": (
        "digit_corrections", "counter_corrections", "duration_valid", "duration_invalid",
        "campaign_double_hyphen", "campaign_i_correction", "campaign_two_digit",
    ),
    "detection_contract": (
        "onnx_boxes_text_scores_order", "onnx_missing_text_and_scores", "ncnn_no_boxes",
    ),
    "queue_cache": (
        "queued_success", "queued_exception_traceback", "reentrant_execution",
        "cache_key_device", "cache_key_model_version",
    ),
    "windows_ml": (
        "cpu_session", "vendor_provider_names", "integrated_gpu_rejected",
        "discrete_gpu_accepted", "device_enumeration_failure",
    ),
    "rpc_security": (
        "loopback_normalization", "remote_address_rejected", "uint8_round_trip",
        "float32_round_trip", "truncated_payload_rejected", "object_dtype_rejected",
    ),
    "debug_privacy": (
        "disabled_is_noop", "safe_filename", "git_root_rejected", "retention_enforced",
    ),
    "benchmark": (
        "fast_rating", "medium_rating", "slow_rating",
    ),
}

RUNTIME_MATRIX_CLASS = (
    "tests.test_stage8b_runtime_scenario_matrix."
    "Stage8BRuntimeScenarioMatrixTests"
)


PRODUCTION_ENTRYPOINTS = {
    "model_selection": "module.ocr.al_ocr._resolve_onnx_model_version",
    "model_files": "module.ocr.ncnn_ocr.NcnnRecOCR._check_model_files",
    "ncnn_output": "module.ocr.ncnn_ocr.NcnnRecOCR._normalize_output",
    "image_preprocess": "module.ocr.al_ocr.AlOcrCtcRecOCR._to_gray",
    "postprocess": "module.ocr.ocr.Digit.after_process",
    "detection_contract": "module.ocr.al_ocr.AlOcr._det_direct",
    "queue_cache": "module.ocr.al_ocr._run_ocr_queued",
    "windows_ml": "module.ocr.windows_ml.create_onnx_session",
    "rpc_security": "module.ocr.stage8b_rpc_security.decode_image_payload",
    "debug_privacy": "module.ocr.stage8b_privacy.save_debug_image",
    "benchmark": "module.daemon.ocr_benchmark.OcrBenchmark._rate_speed",
}

EXPECTED_CONTRACTS = {
    "model_selection": "model version/default/fallback selection is preserved",
    "model_files": "model file and closed-model failures remain explicit",
    "ncnn_output": "NCNN output normalization preserves values, dtype and class axis",
    "image_preprocess": "supported image formats normalize to uint8 grayscale",
    "postprocess": "domain corrections and invalid-duration fallback are preserved",
    "detection_contract": "text, score, boxes and result order remain correlated",
    "queue_cache": "queue traceback/reentrancy and cache-key inputs are preserved",
    "windows_ml": "CPU fallback, provider names and GPU filters are preserved",
    "rpc_security": "RPC is loopback-only and uses a bounded non-pickle ndarray format",
    "debug_privacy": "debug output is opt-in, bounded and outside the repository",
    "benchmark": "speed rating thresholds are unchanged apart from translated labels",
}

BACKEND_COVERAGE = (
    {
        "backend": "ONNX Runtime CPU",
        "ci_level": "PRODUCTION_ENTRYPOINT_FIXTURES",
        "real_acceptance": "CPU_REFERENCE",
    },
    {
        "backend": "Windows ML",
        "ci_level": "PROVIDER_SELECTION_FIXTURES",
        "real_acceptance": "CONFIGURED_PROVIDER_ONLY",
    },
    {
        "backend": "NCNN CPU/Vulkan",
        "ci_level": "NORMALIZATION_AND_SELECTION_FIXTURES",
        "real_acceptance": "WHEN_CONFIGURED",
    },
    {
        "backend": "OCR RPC",
        "ci_level": "LOOPBACK_AND_WIRE_FORMAT_FIXTURES",
        "real_acceptance": "NO_REMOTE_ENDPOINT",
    },
)


def runtime_fixture_test_id(category: str, scenario: str) -> str:
    return f"{RUNTIME_MATRIX_CLASS}.test_{category}__{scenario}"


def scenario_evidence() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for category, scenarios in SCENARIO_REQUIREMENTS.items():
        for scenario in scenarios:
            scenario_id = f"stage8b/{category}/{scenario}"
            if scenario_id in seen:
                raise ValueError(f"Duplicate Stage 8B scenario_id: {scenario_id}")
            seen.add(scenario_id)
            rows.append(
                {
                    "scenario_id": scenario_id,
                    "category": category,
                    "case_id": scenario,
                    "fixture_test": runtime_fixture_test_id(category, scenario),
                    "production_entrypoint": PRODUCTION_ENTRYPOINTS[category],
                    "expected_contract": EXPECTED_CONTRACTS[category],
                    "evidence_level": "CI_PRODUCTION_FIXTURE",
                }
            )
    return rows
