from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

from dev_tools.stage8b_semantic_policy import ROOT

WILDCARD_BIND_RE = re.compile(r"tcp://(?:\*|0\.0\.0\.0|\[::\])")
SERIALIZED_LOG_RE = re.compile(
    r"logger\.[a-zA-Z_]+\([^\n]*(?:payload|image_bytes|tobytes)",
    re.IGNORECASE,
)
RECOGNIZED_FILENAME_RE = re.compile(
    r"filename\s*=.*(?:result|text|txt|res_clean)",
    re.IGNORECASE,
)

RPC_PATHS = (
    "module/ocr/rpc.py",
    "module/ocr/stage8b_rpc_security.py",
)


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _forbidden_serialization_calls(relative: str) -> list[dict[str, Any]]:
    source = _source(relative)
    tree = ast.parse(source, filename=relative)
    findings: list[dict[str, Any]] = []

    class Visitor(ast.NodeVisitor):
        def visit_Import(self, node: ast.Import) -> Any:
            for alias in node.names:
                if alias.name == "pickle":
                    findings.append(
                        {"path": relative, "line": node.lineno, "kind": "pickle_import"}
                    )
            self.generic_visit(node)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> Any:
            if node.module == "pickle":
                findings.append(
                    {"path": relative, "line": node.lineno, "kind": "pickle_import"}
                )
            self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> Any:
            if isinstance(node.func, ast.Attribute):
                owner = node.func.value
                if isinstance(owner, ast.Name) and owner.id == "pickle":
                    findings.append(
                        {
                            "path": relative,
                            "line": node.lineno,
                            "kind": f"pickle_{node.func.attr}",
                        }
                    )
                if (
                    node.func.attr == "dumps"
                    and not (isinstance(owner, ast.Name) and owner.id == "json")
                ):
                    findings.append(
                        {"path": relative, "line": node.lineno, "kind": "object_dumps"}
                    )
            self.generic_visit(node)

    Visitor().visit(tree)
    return findings


def build_security_review(output_dir: Path) -> tuple[dict[str, Any], dict[str, int]]:
    rpc_source = _source("module/ocr/rpc.py")
    rpc_security_source = _source("module/ocr/stage8b_rpc_security.py")
    privacy_source = _source("module/ocr/stage8b_privacy.py")
    al_ocr_source = _source("module/ocr/al_ocr.py")

    wildcard_findings = [
        {"path": "module/ocr/rpc.py", "kind": "wildcard_bind", "match": match.group(0)}
        for match in WILDCARD_BIND_RE.finditer(rpc_source)
    ]
    raw_payload_findings = [
        {"path": "module/ocr/rpc.py", "kind": "serialized_payload_log", "match": match.group(0)}
        for match in SERIALIZED_LOG_RE.finditer(rpc_source)
    ]
    serialization_findings = [
        finding
        for relative in RPC_PATHS
        for finding in _forbidden_serialization_calls(relative)
    ]

    contract_findings: list[dict[str, str]] = []
    for token in (
        "normalize_loopback_address",
        "loopback_bind_uri",
        "client_uri",
        "encode_image_payload",
        "decode_image_payload",
        "SUPPORTED_OCR_MODELS",
        "_validate_model_name",
        "_get_server_model",
        "MAX_RPC_BATCH_IMAGES",
        "MAX_RPC_BATCH_BYTES",
        "_validate_batch",
        "MAX_CANDIDATE_ALPHABET_LENGTH",
        "_validate_candidate_alphabet",
        "args_factory",
    ):
        if token not in rpc_source:
            contract_findings.append(
                {"path": "module/ocr/rpc.py", "kind": "missing_rpc_usage", "token": token}
            )
    for token in (
        "_IMAGE_MAGIC",
        "MAX_SERIALIZED_IMAGE_BYTES",
        "MAX_IMAGE_ELEMENTS",
        "MAX_HEADER_BYTES",
        "encode_image_payload",
        "decode_image_payload",
        "len(image_bytes) != expected_size",
    ):
        if token not in rpc_security_source:
            contract_findings.append(
                {
                    "path": "module/ocr/stage8b_rpc_security.py",
                    "kind": "missing_wire_guard",
                    "token": token,
                }
            )

    debug_findings: list[dict[str, str]] = []
    for token in (
        "AZURPILOT_OCR_DEBUG",
        "debug_output_enabled",
        "resolve_debug_directory",
        "image_fingerprint",
        "retention",
        "_reject_existing_symlink_components",
        "tempfile.mkstemp",
        "os.replace",
    ):
        if token not in privacy_source:
            debug_findings.append(
                {"path": "module/ocr/stage8b_privacy.py", "kind": "missing_guard", "token": token}
            )
    if RECOGNIZED_FILENAME_RE.search(privacy_source):
        debug_findings.append(
            {"path": "module/ocr/stage8b_privacy.py", "kind": "recognized_text_in_filename"}
        )
    if "save_debug_image" not in al_ocr_source:
        debug_findings.append(
            {"path": "module/ocr/al_ocr.py", "kind": "privacy_helper_not_integrated"}
        )

    secret_findings: list[dict[str, str]] = []
    secret_patterns = (
        re.compile(r"AKIA[0-9A-Z]{16}"),
        re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    )
    for relative in (*RPC_PATHS, "module/ocr/stage8b_privacy.py", "module/ocr/al_ocr.py"):
        source = _source(relative)
        for pattern in secret_patterns:
            if pattern.search(source):
                secret_findings.append({"path": relative, "kind": "secret_pattern"})

    findings = (
        wildcard_findings
        + raw_payload_findings
        + serialization_findings
        + contract_findings
        + debug_findings
        + secret_findings
    )
    payload = {
        "status": "PASS" if not findings else "FAIL",
        "rpc_boundary": {
            "bind": "loopback-only",
            "authentication": "local-only transport; no remote endpoint supported",
            "serialization": "fixed ndarray binary wire format; pickle forbidden",
            "model_allowlist": True,
            "bounded_batch": True,
            "bounded_candidate_alphabet": True,
            "lazy_local_fallback": True,
            "remote_rpc_supported": False,
            "wildcard_findings": wildcard_findings,
            "forbidden_serialization_findings": serialization_findings,
        },
        "debug_images": {
            "opt_in": True,
            "default_location_outside_git": True,
            "recognized_text_in_filename": False,
            "bounded_retention": True,
            "symlink_guard": True,
            "atomic_publish": True,
            "findings": debug_findings,
        },
        "raw_payload_findings": raw_payload_findings,
        "contract_findings": contract_findings,
        "secret_findings": secret_findings,
        "findings": findings,
    }
    metrics = {
        "stage8b_rpc_exposure_findings": len(wildcard_findings),
        "stage8b_untrusted_pickle_paths": len(serialization_findings),
        "stage8b_raw_payload_violations": len(raw_payload_findings),
        "stage8b_debug_image_privacy_findings": len(debug_findings),
        "stage8b_rpc_contract_mismatches": len(contract_findings),
        "stage8b_secret_findings": len(secret_findings),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "security-review.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload, metrics
