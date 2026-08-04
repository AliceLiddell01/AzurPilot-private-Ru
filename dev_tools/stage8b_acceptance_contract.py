from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from typing import Any

from dev_tools.stage8b_semantic_policy import ROOT

REQUIRED_REPORT_FIELDS = (
    "head_sha",
    "environment",
    "model_sha256",
    "dictionary_sha256",
    "fixture_archive_sha256",
    "provider_requested_order",
    "provider_registered",
    "provider_session",
    "provider_options",
    "provider_download_performed",
    "fixture_accuracy",
    "cpu_reference",
    "real_values",
    "user_confirmed_values",
    "config_unchanged",
    "temporary_files_removed",
    "debug_images_absent_or_opt_in",
    "rpc_bind",
    "residual_processes",
)

REQUIRED_SOURCE_TOKENS = (
    "psutil",
    "_provider_cache_snapshot",
    "_child_process_snapshot",
    "_environment_fingerprint",
    "_registered_provider_evidence",
    "_session_provider_evidence",
    "_confirm_real_values",
    "user_confirmed_values",
    "temporary_files_removed",
    "provider_download_performed",
)

FORBIDDEN_CONSTANT_SUCCESS = (
    '"provider_download_performed": False',
    '"temporary_files_removed": True',
    '"debug_images_absent_or_opt_in": True',
    '"residual_processes": []',
)


def _return_dict_keys(function: ast.FunctionDef) -> set[str]:
    keys: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Dict):
            continue
        for key in node.value.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                keys.add(key.value)
    return keys


def build_acceptance_contract(output_dir: Path) -> tuple[dict[str, Any], dict[str, int]]:
    path = ROOT / "dev_tools/stage8b_ocr_acceptance.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }
    findings: list[dict[str, Any]] = []
    run = functions.get("run_acceptance")
    if run is None:
        findings.append({"kind": "run_acceptance_missing"})
        report_keys: set[str] = set()
    else:
        report_keys = _return_dict_keys(run)

    for field in REQUIRED_REPORT_FIELDS:
        if field not in report_keys:
            findings.append({"kind": "report_field_missing", "field": field})
    for token in REQUIRED_SOURCE_TOKENS:
        if token not in source:
            findings.append({"kind": "source_guard_missing", "token": token})
    for fragment in FORBIDDEN_CONSTANT_SUCCESS:
        if fragment in source:
            findings.append({"kind": "hardcoded_success", "fragment": fragment})

    provider_fields_distinct = (
        "_registered_provider_evidence" in source
        and "_session_provider_evidence" in source
        and '"provider_registered": registered_provider' in source
        and '"provider_session": session_provider' in source
    )
    if not provider_fields_distinct:
        findings.append({"kind": "provider_fields_not_distinct"})

    interactive_confirmation = (
        "MATCH" in source
        and "user_confirmed_values" in source
        and "confirmed_value_ids" in source
    )
    if not interactive_confirmation:
        findings.append({"kind": "visual_confirmation_not_enforced"})

    payload = {
        "status": "PASS" if not findings else "FAIL",
        "report_fields": sorted(report_keys),
        "required_report_fields": list(REQUIRED_REPORT_FIELDS),
        "provider_fields_distinct": provider_fields_distinct,
        "interactive_visual_confirmation": interactive_confirmation,
        "findings": findings,
    }
    metrics = {"stage8b_acceptance_integrity_findings": len(findings)}
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "acceptance-contract.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload, metrics
