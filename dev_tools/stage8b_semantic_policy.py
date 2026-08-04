from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMMUTABLE_STAGE8B_BASE_SHA = "045162c35ae7583860c88d6d899640ef3a6a1abb"
DEFAULT_OUTPUT_DIR = ROOT / "artifacts" / "stage8b"

# Stage 8B owns only OCR engine/backend/RPC/benchmark diagnostics. Campaign,
# Operation Siren, scheduler and device lifecycle remain owned by their stages.
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
)

TRANSLATION_ONLY_RUNTIME_PATHS = (
    "module/ocr/ncnn_ocr.py",
    "module/ocr/windows_ml.py",
)

APPROVED_BEHAVIOR_RUNTIME_PATHS = (
    "module/ocr/ocr.py",
    "module/daemon/ocr_benchmark.py",
)

SECURITY_RUNTIME_PATHS = (
    "module/ocr/al_ocr.py",
    "module/ocr/rpc.py",
    "module/ocr/stage8b_privacy.py",
    "module/ocr/stage8b_rpc_security.py",
)

PRESERVED_IDENTIFIERS = frozenset(
    {
        "OCR", "RapidOCR", "ONNX Runtime", "Windows ML", "NCNN", "ncnn",
        "Vulkan", "DirectML", "CoreML", "ANE", "NPU", "GPU", "CPU",
        "QNNExecutionProvider", "OpenVINOExecutionProvider", "DmlExecutionProvider",
        "CPUExecutionProvider", "Execution Provider", "EP", "CTC", "CNN",
        "PP-OCRv6", "PID", "RPC", "ZeroRPC", "load_param", "load_model",
        "in0", "out0", "Backend", "benchmark",
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
    "stage8b_compact_spacing_fix_mismatches",
)
