from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

COPIES = {
    "module/ocr/stage8b_privacy.py": "module/ocr/privacy.py",
    "module/ocr/stage8b_rpc_security.py": "module/ocr/rpc_security.py",
    "dev_tools/stage8a_device_acceptance.py": "tools/acceptance/device.py",
    "dev_tools/stage8b_ocr_acceptance.py": "tools/acceptance/ocr.py",
    "dev_tools/stage8b_opsi_zone_acceptance.py": "tools/acceptance/ocr_opsi_zone.py",
    "dev_tools/commission_ocr_acceptance.py": "tools/acceptance/ocr_commission.py",
    "dev_tools/ocr_english_model_benchmark.py": "tools/benchmarks/ocr_english_models.py",
    "dev_tools/screenshot_interval_benchmark.py": "tools/benchmarks/screenshot_intervals.py",
    "dev_tools/stage6_webui_smoke.py": "tools/acceptance/webui_smoke.py",
    "dev_tools/stage8a_binary_log_audit.py": "dev_tools/security/binary_log_audit.py",
    "dev_tools/stage8a_exception_context_audit.py": "dev_tools/security/exception_context_audit.py",
    "tests/test_stage8a_device_acceptance.py": "tests/test_device_acceptance.py",
    "tests/test_stage8a_binary_log_audit.py": "tests/test_binary_log_audit.py",
    "tests/test_stage8a_binary_log_audit_arguments.py": "tests/test_binary_log_audit_arguments.py",
    "tests/test_stage8a_exception_context_audit.py": "tests/test_exception_context_audit.py",
    "tests/test_stage8a_security_review.py": "tests/test_device_security.py",
    "tests/test_stage8b_ocr_acceptance.py": "tests/test_ocr_acceptance.py",
    "tests/test_stage8b_opsi_zone_acceptance.py": "tests/test_opsi_zone_ocr_acceptance.py",
    "tests/test_stage8b_reparse_privacy.py": "tests/test_ocr_debug_privacy.py",
    "tests/test_stage8b_rpc_runtime.py": "tests/test_ocr_rpc_runtime.py",
    "tests/test_stage7_webui_traceback_rendering.py": "tests/test_webui_traceback_security.py",
    "tests/run_stage7_webui_traceback_browser.py": "tests/run_webui_traceback_browser.py",
    "tests/serve_stage7_webui_traceback.py": "tests/serve_webui_traceback.py",
}

REPLACEMENTS = {
    "module.ocr.stage8b_privacy": "module.ocr.privacy",
    "module/ocr/stage8b_privacy.py": "module/ocr/privacy.py",
    "module.ocr.stage8b_rpc_security": "module.ocr.rpc_security",
    "module/ocr/stage8b_rpc_security.py": "module/ocr/rpc_security.py",
    "dev_tools.stage8a_device_acceptance": "tools.acceptance.device",
    "dev_tools/stage8a_device_acceptance.py": "tools/acceptance/device.py",
    "dev_tools.stage8b_ocr_acceptance": "tools.acceptance.ocr",
    "dev_tools/stage8b_ocr_acceptance.py": "tools/acceptance/ocr.py",
    "dev_tools.stage8b_opsi_zone_acceptance": "tools.acceptance.ocr_opsi_zone",
    "dev_tools/stage8b_opsi_zone_acceptance.py": "tools/acceptance/ocr_opsi_zone.py",
    "dev_tools.commission_ocr_acceptance": "tools.acceptance.ocr_commission",
    "dev_tools/commission_ocr_acceptance.py": "tools/acceptance/ocr_commission.py",
    "dev_tools.ocr_english_model_benchmark": "tools.benchmarks.ocr_english_models",
    "dev_tools/ocr_english_model_benchmark.py": "tools/benchmarks/ocr_english_models.py",
    "dev_tools.screenshot_interval_benchmark": "tools.benchmarks.screenshot_intervals",
    "dev_tools/screenshot_interval_benchmark.py": "tools/benchmarks/screenshot_intervals.py",
    "dev_tools.stage6_webui_smoke": "tools.acceptance.webui_smoke",
    "dev_tools/stage6_webui_smoke.py": "tools/acceptance/webui_smoke.py",
    "dev_tools.stage8a_binary_log_audit": "dev_tools.security.binary_log_audit",
    "dev_tools/stage8a_binary_log_audit.py": "dev_tools/security/binary_log_audit.py",
    "dev_tools.stage8a_exception_context_audit": "dev_tools.security.exception_context_audit",
    "dev_tools/stage8a_exception_context_audit.py": "dev_tools/security/exception_context_audit.py",
    "tests.run_stage7_webui_traceback_browser": "tests.run_webui_traceback_browser",
    "tests.serve_stage7_webui_traceback": "tests.serve_webui_traceback",
    "tests/fixtures/stage7_webui_traceback": "tests/fixtures/webui_traceback",
    "fixtures/stage7_webui_traceback": "fixtures/webui_traceback",
}

CLASS_REPLACEMENTS = {
    "Stage8ADeviceAcceptanceTests": "DeviceAcceptanceTests",
    "Stage8ABinaryLogAuditTests": "BinaryLogAuditTests",
    "Stage8ABinaryLogAuditArgumentsTests": "BinaryLogAuditArgumentsTests",
    "Stage8AExceptionContextAuditTests": "ExceptionContextAuditTests",
    "Stage8ASecurityReviewTests": "DeviceSecurityTests",
    "Stage8BOcrAcceptanceTests": "OcrAcceptanceTests",
    "Stage8BReparsePrivacyTests": "OcrDebugPrivacyTests",
    "Stage8BRpcRuntimeTests": "OcrRpcRuntimeTests",
}

AUTHORITATIVE = set(COPIES.values()) | {
    "module/ocr/al_ocr.py",
    "module/ocr/rpc.py",
    "module/daemon/screenshot_interval_benchmark.py",
    ".github/workflows/ci.yml",
    "docs/benchmarks/ocr-english-models.md",
    "docs/benchmarks/screenshot-interval.md",
}


def replace_text(path: Path, replacements: dict[str, str]) -> None:
    text = path.read_text(encoding="utf-8-sig")
    for old, new in replacements.items():
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8", newline="\n")


def main() -> None:
    for source, destination in COPIES.items():
        source_path = ROOT / source
        destination_path = ROOT / destination
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination_path)

    source_fixture = ROOT / "tests/fixtures/stage7_webui_traceback"
    destination_fixture = ROOT / "tests/fixtures/webui_traceback"
    if destination_fixture.exists():
        shutil.rmtree(destination_fixture)
    shutil.copytree(source_fixture, destination_fixture)

    for relative in (
        "tools/__init__.py",
        "tools/acceptance/__init__.py",
        "tools/benchmarks/__init__.py",
        "dev_tools/security/__init__.py",
    ):
        path = ROOT / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('"""AzurPilot developer tooling package."""\n', encoding="utf-8", newline="\n")

    for relative in AUTHORITATIVE:
        path = ROOT / relative
        if path.is_file():
            replace_text(path, REPLACEMENTS)

    ci_path = ROOT / ".github/workflows/ci.yml"
    ci_text = ci_path.read_text(encoding="utf-8")
    for old, new in {
        "tests.test_stage8a_device_acceptance": "tests.test_device_acceptance",
        "tests.test_stage8b_rpc_runtime": "tests.test_ocr_rpc_runtime",
        "tests.test_stage8b_ocr_acceptance": "tests.test_ocr_acceptance",
        "tests.test_stage8b_opsi_zone_acceptance": "tests.test_opsi_zone_ocr_acceptance",
        "tests.test_stage8b_reparse_privacy": "tests.test_ocr_debug_privacy",
        "tests/run_stage7_webui_traceback_browser.py": "tests/run_webui_traceback_browser.py",
    }.items():
        ci_text = ci_text.replace(old, new)
    ci_text = ci_text.replace("          filter: blob:none\n", "")
    ci_path.write_text(ci_text, encoding="utf-8", newline="\n")

    for relative in COPIES.values():
        if relative.startswith("tests/"):
            replace_text(ROOT / relative, CLASS_REPLACEMENTS)

    (ROOT / "tools/acceptance/README.md").write_text(
        "# Acceptance tools\n\n"
        "These commands exercise real external environments and are intentionally outside required CI.\n\n"
        "- `uv run python -m tools.acceptance.device --help` — explicit-target device and control checks.\n"
        "- `uv run python -m tools.acceptance.ocr --help` — local OCR provider and debug-output checks.\n"
        "- `uv run python -m tools.acceptance.ocr_opsi_zone --help` — bounded, read-only Operation Siren zone OCR checks.\n"
        "- `uv run python -m tools.acceptance.ocr_commission --help` — bounded, read-only Commission OCR checks.\n"
        "- `uv run python -m tools.acceptance.webui_smoke --help` — local WebUI startup smoke.\n\n"
        "Acceptance output is local diagnostic data. Do not commit generated reports, screenshots, device identifiers, paths, or external output.\n",
        encoding="utf-8",
        newline="\n",
    )
    (ROOT / "tools/benchmarks/README.md").write_text(
        "# Benchmark tools\n\n"
        "Benchmarks are optional developer commands and are not required status checks.\n\n"
        "- `uv run python -m tools.benchmarks.ocr_english_models --help`\n"
        "- `uv run python -m tools.benchmarks.screenshot_intervals --help`\n\n"
        "Hardware, emulator, and game measurements are environment-specific. Keep generated reports and screenshots out of version control. Fast parser and formatting regressions remain covered by the `Python` job.\n",
        encoding="utf-8",
        newline="\n",
    )


if __name__ == "__main__":
    main()
