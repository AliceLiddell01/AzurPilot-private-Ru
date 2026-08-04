from __future__ import annotations

from typing import Any

from dev_tools.stage8b_semantic_policy import PROMPT_SCENARIO_COUNT

# The original compact matrix remains available for regression tests that
# import it directly. The authoritative Stage 8B prompt matrix is the 171-case
# FULL_SCENARIO_REQUIREMENTS inventory below.
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

FULL_SCENARIO_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "model_selection": (
        "auto_default",
        "explicit_supported",
        "unsupported_fallback",
        "unsupported_model_rejected",
        "recognition_only_pipeline_fallback",
        "english_only_registry",
        "deleted_cjk_model_rejected",
        "backend_auto_resolves",
        "model_version_cache_key",
        "unknown_version_does_not_mutate_config",
    ),
    "model_files": (
        "english_v66_model_present",
        "english_v65_model_present",
        "ctc_900k_model_present",
        "english_dictionary_present",
        "detector_present",
        "missing_model_rejected",
        "missing_dictionary_rejected",
        "hash_stable",
        "closed_model_rejected",
        "removed_cjk_assets_absent",
    ),
    "onnx_runtime": (
        "cpu_session",
        "session_options_factory",
        "provider_order_cpu",
        "provider_order_gpu",
        "inference_uint8",
        "inference_float32",
        "invalid_rank",
        "invalid_channel_count",
        "dynamic_width",
        "max_width_clamp",
        "session_run_failure_propagates",
        "missing_input_name",
        "malformed_output_shape",
        "ctc_decode_blank",
        "ctc_decode_repeat",
        "ctc_score_stable",
    ),
    "windows_ml": (
        "cpu_fallback",
        "vendor_provider_names_auto",
        "vendor_provider_names_gpu",
        "qnn_priority",
        "openvino_gpu_priority",
        "openvino_cpu_priority",
        "integrated_gpu_rejected",
        "discrete_gpu_accepted",
        "device_enumeration_failure",
        "provider_prepare_disabled",
        "ensure_ready_success",
        "ensure_ready_failure",
        "register_missing_provider",
        "already_registered_provider",
        "session_creation_fallback_next_device",
        "provider_evidence_distinguishes_registered_and_session",
    ),
    "ncnn": (
        "supported_model",
        "unsupported_model_rejected",
        "cpu_net_creation",
        "vulkan_net_creation",
        "missing_param",
        "missing_bin",
        "missing_dictionary",
        "closed_model",
        "time_class_matrix",
        "class_time_matrix",
        "batched_time_class",
        "invalid_shape",
        "preprocess_gray",
        "preprocess_bgr",
        "extract_failure",
        "release_resources",
    ),
    "rapidocr": (
        "rec_only_disables_detection",
        "rec_only_disables_classification",
        "text_rec_output_text",
        "text_rec_output_score",
        "word_results_preserved",
        "elapsed_preserved",
        "empty_output",
        "missing_text",
        "missing_scores",
        "malformed_output",
        "recognition_only_pipeline",
        "detector_only_pipeline",
        "load_image_path",
        "load_image_ndarray",
        "rotated_crop_order",
        "output_dataclass_contract",
    ),
    "detection": (
        "boxes_text_scores_order",
        "missing_text_defaults",
        "missing_scores_defaults",
        "no_boxes_empty",
        "box_dtype_preserved",
        "box_coordinates_preserved",
        "result_order_preserved",
        "ncnn_detection_hybrid",
        "save_debug_disabled",
        "save_debug_no_text_filename",
    ),
    "queue": (
        "queued_success",
        "exception_traceback",
        "reentrant_execution",
        "concurrent_submissions",
        "task_done_called",
        "worker_started_once",
        "worker_ident_set",
        "no_deadlock",
        "result_order",
        "shutdown_independent",
    ),
    "cache": (
        "model_cache_miss",
        "model_cache_hit",
        "device_in_key",
        "backend_in_key",
        "version_in_key",
        "vendor_ep_in_key",
        "release_closes_models",
        "release_close_failure_logged",
        "reset_delegates_release",
        "detector_cache_separate",
    ),
    "ocr_classes": (
        "ocr_default_lang",
        "ocr_custom_lang",
        "digit_returns_int",
        "digit_empty_zero",
        "digit_yuv_equivalence",
        "counter_triplet",
        "counter_invalid_zeroes",
        "counter_yuv_equivalence",
        "duration_valid",
        "duration_compact",
        "duration_invalid_zero",
        "duration_yuv_equivalence",
    ),
    "postprocess": (
        "digit_corrections",
        "counter_corrections",
        "campaign_double_hyphen",
        "campaign_i_correction",
        "campaign_two_digit",
        "compact_colon_numeric",
        "compact_slash_numeric",
        "compact_hyphen_numeric",
        "preserve_words",
        "preserve_phrase",
        "preserve_other_model",
        "low_confidence_no_global_strip",
    ),
    "rpc": (
        "loopback_normalization",
        "ipv6_loopback_normalization",
        "remote_rejected",
        "wildcard_rejected",
        "port_range",
        "uint8_round_trip",
        "float32_round_trip",
        "truncated_rejected",
        "corrupt_header_rejected",
        "object_dtype_rejected",
        "model_allowlist",
        "attribute_traversal_rejected",
        "batch_count_limit",
        "batch_bytes_limit",
        "candidate_alphabet_limit",
        "transport_failure_local_fallback",
    ),
    "benchmark": (
        "english_only_benchmark_matrix",
        "archive_discovery",
        "dataset_loading",
        "missing_dataset",
        "accuracy_count",
        "exact_output_comparison",
        "speed_fast",
        "speed_medium",
        "speed_slow",
        "cleanup_temp_dir",
    ),
    "false_recognition": (
        "positive_compact_colon_crop",
        "positive_counter_crop",
        "positive_duration_crop",
        "positive_stage_crop",
        "negative_ship_name_crop",
        "transition_frame_fixture",
        "low_confidence_fixture",
    ),
}

FULL_RUNTIME_MATRIX_CLASS = (
    "tests.test_stage8b_prompt_scenario_matrix."
    "Stage8BPromptScenarioMatrixTests"
)

PRODUCTION_ENTRYPOINTS = {
    "model_selection": "module.ocr.al_ocr._resolve_onnx_model_version",
    "model_files": "module.ocr.al_ocr.ONNX_MODEL_PARAMS",
    "onnx_runtime": "module.ocr.al_ocr.AlOcrCtcRecOCR",
    "windows_ml": "module.ocr.windows_ml.create_onnx_session",
    "ncnn": "module.ocr.ncnn_ocr.NcnnRecOCR",
    "rapidocr": "module.ocr.al_ocr.RecOnlyOCR",
    "detection": "module.ocr.al_ocr.AlOcr._det_direct",
    "queue": "module.ocr.al_ocr._run_ocr_queued",
    "cache": "module.ocr.al_ocr._get_model",
    "ocr_classes": "module.ocr.ocr.Ocr",
    "postprocess": "module.ocr.ocr.normalize_ocr_text",
    "rpc": "module.ocr.rpc.ModelProxy",
    "benchmark": "module.daemon.ocr_benchmark.OcrBenchmark",
    "false_recognition": "dev_tools.stage8b_real_output_contract.build_real_output_contract",
}

EXPECTED_CONTRACTS = {
    "model_selection": "only Global/English models and versions are selectable",
    "model_files": "required English assets exist and removed CJK assets stay absent",
    "onnx_runtime": "CPU inference, preprocessing and CTC decoding remain deterministic",
    "windows_ml": "requested, registered and session providers are distinguished",
    "ncnn": "English NCNN lifecycle, preprocessing and output normalization remain valid",
    "rapidocr": "RapidOCR output fields and recognition-only pipelines remain compatible",
    "detection": "text, scores, boxes and result order stay correlated",
    "queue": "queue ordering, traceback, task completion and reentrancy remain safe",
    "cache": "cache keys, hits, release and reset preserve lifecycle contracts",
    "ocr_classes": "Ocr, Digit, Counter, Duration and YUV variants preserve outputs",
    "postprocess": "only approved compact numeric spacing is normalized",
    "rpc": "RPC is loopback-only, bounded, allowlisted and non-pickle",
    "benchmark": "benchmark compares only Global/English versions with explicit metadata",
    "false_recognition": "real and negative fixtures constrain approved spacing behavior",
}

BACKEND_COVERAGE = (
    {
        "backend": "ONNX Runtime CPU",
        "ci_level": "REAL_BUNDLED_FIXTURE_INFERENCE",
        "real_acceptance": "CPU_REFERENCE",
    },
    {
        "backend": "Windows ML",
        "ci_level": "PROVIDER_SELECTION_AND_REGISTRATION_FIXTURES",
        "real_acceptance": "CONFIGURED_PROVIDER_REQUIRED",
    },
    {
        "backend": "NCNN CPU/Vulkan",
        "ci_level": "MODEL_LIFECYCLE_AND_OUTPUT_FIXTURES",
        "real_acceptance": "WHEN_CONFIGURED",
    },
    {
        "backend": "OCR RPC",
        "ci_level": "LOOPBACK_WIRE_FORMAT_AND_FALLBACK_FIXTURES",
        "real_acceptance": "NO_REMOTE_ENDPOINT",
    },
)


def full_runtime_fixture_test_id(category: str, scenario: str) -> str:
    return f"{FULL_RUNTIME_MATRIX_CLASS}.test_{category}__{scenario}"


def scenario_evidence() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for category, scenarios in FULL_SCENARIO_REQUIREMENTS.items():
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
                    "fixture_test": full_runtime_fixture_test_id(category, scenario),
                    "production_entrypoint": PRODUCTION_ENTRYPOINTS[category],
                    "expected_contract": EXPECTED_CONTRACTS[category],
                    "evidence_level": (
                        "CI_REAL_FIXTURE"
                        if category in {"onnx_runtime", "false_recognition"}
                        else "CI_PRODUCTION_ENTRYPOINT_FIXTURE"
                    ),
                }
            )
    if len(rows) != PROMPT_SCENARIO_COUNT:
        raise ValueError(
            "Stage 8B prompt scenario inventory mismatch: "
            f"expected {PROMPT_SCENARIO_COUNT}, got {len(rows)}"
        )
    return rows
