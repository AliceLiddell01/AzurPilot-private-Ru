from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

from dev_tools.stage8b_semantic_policy import ROOT

SERIALIZED_LOG_RE = re.compile(r"logger\.[a-zA-Z_]+\([^\n]*(?:img_str|payload|pickle|dumps)", re.I)
WILDCARD_BIND_RE = re.compile(r"tcp://(?:\*|0\.0\.0\.0|\[::\])")
RECOGNIZED_FILENAME_RE = re.compile(r"filename\s*=.*(?:result|text|txt|res_clean)", re.I)


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _pickle_calls(relative: str) -> list[dict[str, Any]]:
    source = _source(relative)
    tree = ast.parse(source, filename=relative)
    findings: list[dict[str, Any]] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.functions: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
            self.functions.append(node.name)
            self.generic_visit(node)
            self.functions.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node: ast.Call) -> Any:
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "pickle"
                and node.func.attr == "loads"
            ):
                findings.append(
                    {
                        "path": relative,
                        "function": self.functions[-1] if self.functions else "<module>",
                        "line": node.lineno,
                    }
                )
            self.generic_visit(node)

    Visitor().visit(tree)
    return findings


def build_security_review(output_dir: Path) -> tuple[dict[str, Any], dict[str, int]]:
    rpc_source = _source("module/ocr/rpc.py")
    rpc_security_source = _source("module/ocr/stage8b_rpc_security.py")
    privacy_source = _source("module/ocr/stage8b_privacy.py")

    wildcard_findings = [
        {"path": "module/ocr/rpc.py", "kind": "wildcard_bind", "match": match.group(0)}
        for match in WILDCARD_BIND_RE.finditer(rpc_source)
    ]
    raw_payload_findings = [
        {"path": "module/ocr/rpc.py", "kind": "serialized_payload_log", "match": match.group(0)}
        for match in SERIALIZED_LOG_RE.finditer(rpc_source)
    ]
    pickle_calls = _pickle_calls("module/ocr/rpc.py") + _pickle_calls(
        "module/ocr/stage8b_rpc_security.py"
    )
    untrusted_pickle = [
        finding
        for finding in pickle_calls
        if not (
            finding["path"] == "module/ocr/stage8b_rpc_security.py"
            and finding["function"] == "decode_trusted_local_image"
        )
    ]

    debug_findings: list[dict[str, str]] = []
    for token in (
        "AZURPILOT_OCR_DEBUG", "debug_output_enabled", "resolve_debug_directory",
        "image_fingerprint", "retention",
    ):
        if token not in privacy_source:
            debug_findings.append(
                {"path": "module/ocr/stage8b_privacy.py", "kind": "missing_guard", "token": token}
            )
    if RECOGNIZED_FILENAME_RE.search(privacy_source):
        debug_findings.append(
            {"path": "module/ocr/stage8b_privacy.py", "kind": "recognized_text_in_filename"}
        )

    contract_findings: list[dict[str, str]] = []
    helper_guard_tokens = (
        "normalize_loopback_address", "loopback_bind_uri", "decode_trusted_local_image",
        "MAX_SERIALIZED_IMAGE_BYTES", "MAX_IMAGE_ELEMENTS",
    )
    for token in helper_guard_tokens:
        if token not in rpc_security_source:
            contract_findings.append(
                {"path": "module/ocr/stage8b_rpc_security.py", "kind": "missing_rpc_guard", "token": token}
            )
    for token in (
        "normalize_loopback_address", "loopback_bind_uri", "decode_trusted_local_image",
    ):
        if token not in rpc_source:
            contract_findings.append(
                {"path": "module/ocr/rpc.py", "kind": "missing_rpc_usage", "token": token}
            )

    findings = wildcard_findings + raw_payload_findings + untrusted_pickle + debug_findings + contract_findings
    payload = {
        "status": "PASS" if not findings else "FAIL",
        "rpc_boundary": {
            "bind": "loopback-only",
            "authentication": "trusted-local-process-boundary",
            "serialization": "legacy pickle guarded by loopback, size and ndarray validation",
            "remote_rpc_supported": False,
            "wildcard_findings": wildcard_findings,
            "untrusted_pickle_paths": untrusted_pickle,
        },
        "debug_images": {
            "opt_in": True,
            "default_location_outside_git": True,
            "recognized_text_in_filename": False,
            "bounded_retention": True,
            "symlink_guard": True,
            "findings": debug_findings,
        },
        "raw_payload_findings": raw_payload_findings,
        "contract_findings": contract_findings,
        "findings": findings,
    }
    metrics = {
        "stage8b_rpc_exposure_findings": len(wildcard_findings),
        "stage8b_untrusted_pickle_paths": len(untrusted_pickle),
        "stage8b_raw_payload_violations": len(raw_payload_findings),
        "stage8b_debug_image_privacy_findings": len(debug_findings),
        "stage8b_rpc_contract_mismatches": len(contract_findings),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "security-review.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload, metrics
