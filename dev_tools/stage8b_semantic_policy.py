from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMMUTABLE_STAGE8B_BASE_SHA = "045162c35ae7583860c88d6d899640ef3a6a1abb"
DEFAULT_OUTPUT_DIR = ROOT / "artifacts" / "stage8b"
PROMPT_SCENARIO_COUNT = 171

# Stage 8B owns the OCR engine/backend/RPC/benchmark contour and only the
# OCR-owned runtime messages in campaign, Operation Siren and device modules.
# External modules are filtered by function owner in Stage8BOcrLogAudit so the
# stage cannot claim unrelated control-flow or localization work.
OCR_SCOPE_PATHS = (
    "module/ocr/al_ocr.py",
    "module/ocr/ocr.py",
    "module/ocr/models.py",
    "module/ocr/ncnn_ocr.py",
    "module/ocr/windows_ml.py",
    "module/ocr/rpc.py",
    "module/ocr/stage8b_privacy.py",
    "module/ocr/stage8b_rpc_security.py",
    "module/daemon/ocr_benchmark.py",
    "module/campaign/campaign_ocr.py",
    "module/os/sea_miles_ocr.py",
    "module/device/device.py",
)

OCR_SCOPE_RULES: dict[str, tuple[str, ...] | None] = {
    "module/campaign/campaign_ocr.py": (
        "CampaignOcr._campaign_ocr_result_process",
        "CampaignOcr._campaign_separate_name",
        "CampaignOcr._extract_stage_name",
        "CampaignOcr._get_stage_name",
    ),
    "module/os/sea_miles_ocr.py": ("SeaMilesOCR.after_process",),
    "module/device/device.py": ("Device.run_simple_ocr_benchmark",),
}

TRANSLATION_ONLY_RUNTIME_PATHS = (
    "module/ocr/ncnn_ocr.py",
    "module/ocr/windows_ml.py",
    "module/campaign/campaign_ocr.py",
    "module/os/sea_miles_ocr.py",
    "module/device/device.py",
)

APPROVED_BEHAVIOR_RUNTIME_PATHS = (
    "module/ocr/ocr.py",
    "module/daemon/ocr_benchmark.py",
    "module/ocr/models.py",
    "module/config/argument/argument.yaml",
)

SECURITY_RUNTIME_PATHS = (
    "module/ocr/al_ocr.py",
    "module/ocr/rpc.py",
    "module/ocr/stage8b_privacy.py",
    "module/ocr/stage8b_rpc_security.py",
)

ENGLISH_ONLY_MODEL_NAMES = frozenset({"azur_lane"})
REMOVED_MODEL_NAMES = frozenset(
    {"azur_lane_jp", "cn", "cnocr", "jp", "tw", "ppocr_v6"}
)

PRESERVED_IDENTIFIERS = frozenset(
    {
        "OCR", "RapidOCR", "ONNX Runtime", "Windows ML", "NCNN", "ncnn",
        "Vulkan", "DirectML", "CoreML", "ANE", "NPU", "GPU", "CPU",
        "QNNExecutionProvider", "OpenVINOExecutionProvider", "DmlExecutionProvider",
        "CPUExecutionProvider", "Execution Provider", "EP", "CTC", "CNN",
        "PP-OCRv6", "PID", "RPC", "ZeroRPC", "load_param", "load_model",
        "in0", "out0", "Backend", "benchmark", "Global", "EN",
    }
)

BLOCKING_METRICS = (
    "stage8b_unresolved", "stage8b_cjk_first_party_remaining",
    "stage8b_english_first_party_remaining", "stage8b_placeholder_mismatches",
    "stage8b_severity_mismatches", "stage8b_sequence_mismatches",
    "stage8b_control_flow_mismatches", "stage8b_output_value_mismatches",
    "stage8b_score_mismatches", "stage8b_box_mismatches",
    "stage8b_result_order_mismatches", "stage8b_model_selection_mismatches",
    "stage8b_model_version_mismatches", "stage8b_provider_order_mismatches",
    "stage8b_backend_fallback_mismatches", "stage8b_threshold_mismatches",
    "stage8b_alphabet_mismatches", "stage8b_cache_key_mismatches",
    "stage8b_queue_contract_mismatches", "stage8b_rpc_contract_mismatches",
    "stage8b_rpc_exposure_findings", "stage8b_untrusted_pickle_paths",
    "stage8b_environment_fingerprint_mismatches", "stage8b_acceptance_head_mismatches",
    "stage8b_raw_payload_violations", "stage8b_debug_image_privacy_findings",
    "stage8b_secret_findings", "stage8b_mojibake_findings", "stage8b_scenario_missing",
    "stage8b_scenario_count_mismatches", "stage8b_compact_spacing_fix_mismatches",
    "stage8b_real_output_text_mismatches", "stage8b_real_output_score_mismatches",
    "stage8b_real_output_box_mismatches", "stage8b_real_output_order_mismatches",
    "stage8b_real_fixture_hash_mismatches", "stage8b_real_model_hash_mismatches",
    "stage8b_real_environment_mismatches", "stage8b_model_scope_findings",
    "stage8b_acceptance_integrity_findings", "stage8b_required_gate_findings",
)
