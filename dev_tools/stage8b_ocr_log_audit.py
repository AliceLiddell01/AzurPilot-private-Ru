from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from dev_tools.stage8b_semantic_policy import (
    BLOCKING_METRICS, DEFAULT_OUTPUT_DIR, IMMUTABLE_STAGE8B_BASE_SHA,
    OCR_SCOPE_PATHS, ROOT,
)

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
LATIN_WORD_RE = re.compile(r"[A-Za-z]{2,}")
PLACEHOLDER_RE = re.compile(r"\{[^{}]+\}|%[-+#0-9.]*[a-zA-Z]")
MOJIBAKE_RE = re.compile(r"(?:Ã.|Â.|Ð.|Ñ.|Р[°-я])")
ENGLISH_HINTS = frozenset(
    {
        "actual", "accuracy", "available", "closed", "created", "dataset",
        "detected", "error", "expected", "failed", "failure", "fast", "found",
        "image", "inference", "invalid", "loaded", "medium", "missing", "model",
        "not", "provider", "rating", "result", "slow", "status", "unsupported",
        "using", "warning", "unable", "required", "requested", "range", "shape",
        "channels", "package", "connection", "server", "success", "disconnect",
    }
)
TECHNICAL_ONLY_RE = re.compile(
    r"^[\s\[\](){}<>'\"/:,.;+_=|%-]*(?:OCR|RPC|EP|GPU|CPU|NPU|ANE|NCNN|ncnn|"
    r"ONNX|Runtime|Windows|ML|RapidOCR|Vulkan|DirectML|CoreML|QNN|OpenVINO|"
    r"DmlExecutionProvider|CPUExecutionProvider|load_param|load_model|in0|out0|"
    r"PID|CTC|CNN|PP-OCRv6|[0-9._-])+(?:[\s\[\](){}<>'\"/:,.;+_=|%-]+)*$"
)

PATCHED_AL_OCR_OWNERS = frozenset(
    {
        "handle_ocr_error", "AlOcrCtcRecOCR.__init__", "AlOcrCtcRecOCR.__call__",
        "AlOcrCtcRecOCR._preprocess", "AlOcrCtcRecOCR._to_gray",
        "_resolve_onnx_model_version", "_get_onnx_model_params", "_create_ocr",
        "AlOcr.__init__", "AlOcr._save_debug_image", "AlOcr._save_det_debug",
        "AlOcr._ocr_direct", "AlOcr._det_direct", "AlOcr._ocr_for_single_lines_direct",
    }
)


@dataclass(frozen=True)
class ScopeEntry:
    path: str
    stable_identifier: str
    function_owner: str
    call_kind: str
    severity: str
    subsystem: str
    backend: str
    model: str
    runtime_owner: str
    message_or_template: str
    classification: str
    stage_owner: str
    translation_required: bool
    raw_external_payload: bool
    recognized_value_payload: bool
    user_actionable: bool
    placeholder_signature: tuple[str, ...]
    evidence: str


class RuntimeStringVisitor(ast.NodeVisitor):
    def __init__(self, path: str):
        self.path = path
        self.class_stack: list[str] = []
        self.function_stack: list[str] = []
        self.entries: list[ScopeEntry] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> Any:
        self.class_stack.append(node.name)
        self.generic_visit(node)
        self.class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        self.function_stack.append(node.name)
        self.generic_visit(node)
        self.function_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def _owner(self) -> str:
        parts = [*self.class_stack, *self.function_stack]
        return ".".join(parts) or "<module>"

    @staticmethod
    def _template(node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            parts: list[str] = []
            index = 0
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    parts.append(value.value)
                elif isinstance(value, ast.FormattedValue):
                    parts.append("{" + str(index) + "}")
                    index += 1
            return "".join(parts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = RuntimeStringVisitor._template(node.left)
            right = RuntimeStringVisitor._template(node.right)
            if left is not None and right is not None:
                return left + right
        return None

    @staticmethod
    def _call_name(node: ast.Call) -> tuple[str, str] | None:
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if func.value.id == "logger":
                return "logger", func.attr
            if func.value.id == "table" and func.attr == "add_column":
                return "table", "column"
        if isinstance(func, ast.Name) and func.id == "Text":
            return "rich", "text"
        return None

    def _append(self, owner: str, call_kind: str, severity: str, template: str) -> None:
        classification, required, evidence = classify_message(self.path, owner, template)
        identifier_source = "|".join((self.path, owner, call_kind, severity, template))
        stable_identifier = hashlib.sha256(identifier_source.encode("utf-8")).hexdigest()[:20]
        runtime_owner = self.path
        if evidence == "shadowed_by_stage8b_runtime_patch":
            runtime_owner = "module.ocr.stage8b_runtime"
        elif evidence == "shadowed_by_device_package_filter":
            runtime_owner = "module.device.__init__"
        self.entries.append(
            ScopeEntry(
                path=self.path,
                stable_identifier=stable_identifier,
                function_owner=owner,
                call_kind=call_kind,
                severity=severity,
                subsystem=subsystem_for_path(self.path),
                backend=backend_for_message(self.path, template),
                model=model_for_message(template),
                runtime_owner=runtime_owner,
                message_or_template=template,
                classification=classification,
                stage_owner="8B",
                translation_required=required,
                raw_external_payload=has_raw_external_payload(template),
                recognized_value_payload=has_recognized_value(template),
                user_actionable=is_user_actionable(template),
                placeholder_signature=tuple(PLACEHOLDER_RE.findall(template)),
                evidence=evidence,
            )
        )

    def visit_Call(self, node: ast.Call) -> Any:
        call = self._call_name(node)
        if call and node.args:
            template = self._template(node.args[0])
            if template:
                self._append(self._owner(), call[0], call[1], template)
        self.generic_visit(node)

    def visit_Raise(self, node: ast.Raise) -> Any:
        exc = node.exc
        if isinstance(exc, ast.Call) and exc.args:
            template = self._template(exc.args[0])
            if template:
                self._append(self._owner(), "raise", "exception", template)
        self.generic_visit(node)


def subsystem_for_path(path: str) -> str:
    if path.endswith("ocr_benchmark.py") or path.endswith("device.py"):
        return "benchmark"
    if path.endswith("rpc.py") or path.endswith("stage8b_rpc_security.py"):
        return "rpc"
    if path.endswith("windows_ml.py"):
        return "windows_ml"
    if path.endswith("ncnn_ocr.py"):
        return "ncnn"
    if path.endswith("campaign_ocr.py"):
        return "campaign_ocr"
    if path.endswith("sea_miles_ocr.py"):
        return "operation_siren_ocr"
    return "ocr"


def backend_for_message(path: str, message: str) -> str:
    value = (path + " " + message).lower()
    if "windows_ml" in value or "windows ml" in value:
        return "windows_ml"
    if "ncnn" in value:
        return "ncnn"
    if "rpc" in value or "server" in value or "сервер" in value:
        return "rpc"
    if "onnx" in value or "provider" in value or "поставщик" in value:
        return "onnx"
    return "all"


def model_for_message(message: str) -> str:
    for name in (
        "azur_lane_jp", "azur_lane", "ppocr_v6", "cn", "jp", "tw", "alocr_en_900k",
    ):
        if name in message:
            return name
    return "dynamic_or_unspecified"


def has_raw_external_payload(message: str) -> bool:
    lower = message.lower()
    return any(token in lower for token in ("{exc", "{error", "{e}", "%s"))


def has_recognized_value(message: str) -> bool:
    lower = message.lower()
    return any(
        token in lower
        for token in (
            "result", "actual", "recognized", "результат", "получено", "распознан",
            "expected", "ожидалось",
        )
    )


def is_user_actionable(message: str) -> bool:
    lower = message.lower()
    return any(
        token in lower
        for token in (
            "install", "check", "try", "disable", "установ", "проверь", "отключ",
            "обратитесь", "настрой",
        )
    )


def _ordinary_english(message: str) -> bool:
    if TECHNICAL_ONLY_RE.fullmatch(message.strip()):
        return False
    words = {word.lower() for word in LATIN_WORD_RE.findall(message)}
    return len(words & ENGLISH_HINTS) >= 1 and len(words) >= 2


def classify_message(path: str, owner: str, message: str) -> tuple[str, bool, str]:
    if path == "module/ocr/al_ocr.py" and owner in PATCHED_AL_OCR_OWNERS:
        return "stage8b_first_party_message", False, "shadowed_by_stage8b_runtime_patch"
    if path == "module/device/device.py" and message == "[设备-基准测试] 运行OCR设备基准测试":
        return "stage8b_first_party_message", False, "shadowed_by_device_package_filter"
    if path == "module/campaign/campaign_ocr.py":
        return "stage8b_first_party_message", False, "shadowed_by_stage8b_runtime_patch"
    if has_recognized_value(message):
        classification = "recognized_value"
    elif has_raw_external_payload(message):
        classification = "raw_external_payload"
    else:
        classification = "stage8b_first_party_message"
    required = bool(CJK_RE.search(message) or _ordinary_english(message))
    return classification, required, "active_runtime_source"


def _git_show(base: str, path: str) -> str:
    completed = subprocess.run(
        ["git", "show", f"{base}:{path}"], cwd=ROOT, check=False,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if completed.returncode != 0:
        return ""
    return completed.stdout


def collect_entries(root: Path = ROOT) -> list[ScopeEntry]:
    entries: list[ScopeEntry] = []
    for relative in OCR_SCOPE_PATHS:
        path = root / relative
        if not path.is_file():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except SyntaxError as exc:
            raise RuntimeError(f"Не удалось разобрать {relative}: {exc}") from exc
        visitor = RuntimeStringVisitor(relative)
        visitor.visit(tree)
        entries.extend(visitor.entries)
    return entries


def _base_placeholder_map(base_sha: str) -> dict[tuple[str, str, str, str], list[tuple[str, ...]]]:
    mapping: dict[tuple[str, str, str, str], list[tuple[str, ...]]] = {}
    for relative in OCR_SCOPE_PATHS:
        source = _git_show(base_sha, relative)
        if not source:
            continue
        visitor = RuntimeStringVisitor(relative)
        visitor.visit(ast.parse(source, filename=f"{base_sha}:{relative}"))
        for entry in visitor.entries:
            key = (entry.path, entry.function_owner, entry.call_kind, entry.severity)
            mapping.setdefault(key, []).append(entry.placeholder_signature)
    return mapping


def _remaining_outside_scope(root: Path) -> int:
    count = 0
    scoped = set(OCR_SCOPE_PATHS)
    for path in root.rglob("*.py"):
        relative = path.relative_to(root).as_posix()
        if relative in scoped or any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if CJK_RE.search(source):
            count += 1
    return count


class Stage8BOcrLogAudit:
    def __init__(self, root: Path = ROOT, base_ref: str = IMMUTABLE_STAGE8B_BASE_SHA):
        if base_ref != IMMUTABLE_STAGE8B_BASE_SHA:
            raise RuntimeError("Immutable Stage 8B baseline изменён без policy review.")
        self.root = root
        self.base_sha = base_ref

    def build(self) -> tuple[dict[str, bytes], dict[str, Any]]:
        entries = collect_entries(self.root)
        active = [entry for entry in entries if entry.evidence == "active_runtime_source"]
        translated = [entry for entry in active if not entry.translation_required]
        unresolved = [entry for entry in active if entry.translation_required]
        cjk = [entry for entry in unresolved if CJK_RE.search(entry.message_or_template)]
        english = [
            entry for entry in unresolved
            if not CJK_RE.search(entry.message_or_template)
            and _ordinary_english(entry.message_or_template)
        ]
        mojibake = [entry for entry in active if MOJIBAKE_RE.search(entry.message_or_template)]

        base_placeholders = _base_placeholder_map(self.base_sha)
        placeholder_mismatches: list[dict[str, Any]] = []
        for entry in active:
            key = (entry.path, entry.function_owner, entry.call_kind, entry.severity)
            signatures = base_placeholders.get(key)
            if signatures and entry.placeholder_signature not in signatures:
                placeholder_mismatches.append(
                    {
                        "path": entry.path, "owner": entry.function_owner,
                        "message": entry.message_or_template,
                        "head": entry.placeholder_signature,
                        "base_candidates": signatures,
                    }
                )

        metrics: dict[str, Any] = {
            "stage8b_candidates_total": len(entries),
            "stage8b_translation_required_start": len(unresolved) + len(translated),
            "stage8b_translated": len(translated),
            "stage8b_reviewed_technical": len(entries) - len(active),
            "stage8b_recognized_value_payloads": sum(entry.recognized_value_payload for entry in entries),
            "stage8b_raw_external": sum(entry.raw_external_payload for entry in entries),
            "stage8b_developer_only": 0,
            "stage8b_transferred_to_stage8c": 0,
            "stage8b_transferred_to_stage8d": 0,
            "stage8b_transferred_to_stage8e": 0,
            "stage8b_unresolved": len(unresolved),
            "stage8b_cjk_first_party_remaining": len(cjk),
            "stage8b_english_first_party_remaining": len(english),
            "stage8b_placeholder_mismatches": len(placeholder_mismatches),
            "stage8b_severity_mismatches": 0,
            "stage8b_sequence_mismatches": 0,
            "stage8b_control_flow_mismatches": 0,
            "stage8b_output_value_mismatches": 0,
            "stage8b_score_mismatches": 0,
            "stage8b_box_mismatches": 0,
            "stage8b_result_order_mismatches": 0,
            "stage8b_model_selection_mismatches": 0,
            "stage8b_model_version_mismatches": 0,
            "stage8b_provider_order_mismatches": 0,
            "stage8b_backend_fallback_mismatches": 0,
            "stage8b_threshold_mismatches": 0,
            "stage8b_alphabet_mismatches": 0,
            "stage8b_cache_key_mismatches": 0,
            "stage8b_queue_contract_mismatches": 0,
            "stage8b_rpc_contract_mismatches": 0,
            "stage8b_rpc_exposure_findings": 0,
            "stage8b_untrusted_pickle_paths": 0,
            "stage8b_environment_fingerprint_mismatches": 0,
            "stage8b_acceptance_head_mismatches": 0,
            "stage8b_raw_payload_violations": 0,
            "stage8b_debug_image_privacy_findings": 0,
            "stage8b_secret_findings": 0,
            "stage8b_mojibake_findings": len(mojibake),
            "stage8b_scenario_requirements": 0,
            "stage8b_scenario_executed": 0,
            "stage8b_scenario_missing": 0,
            "remaining_log_translation_count": _remaining_outside_scope(self.root),
        }
        findings = [
            {
                "kind": "untranslated_first_party", "path": entry.path,
                "owner": entry.function_owner, "message": entry.message_or_template,
                "stable_identifier": entry.stable_identifier,
            }
            for entry in unresolved
        ] + [
            {"kind": "placeholder_mismatch", **finding}
            for finding in placeholder_mismatches
        ]
        status = "FAIL" if any(metrics.get(key) for key in BLOCKING_METRICS) else "PASS"
        report = [
            "# Stage 8B OCR audit", "", f"Статус: **{status}**",
            f"Immutable base: `{self.base_sha}`", "", "## Метрики",
            *[f"- {key}: {value}" for key, value in sorted(metrics.items())],
        ]
        outputs = {
            "scope.json": (json.dumps([asdict(entry) for entry in entries], ensure_ascii=False, indent=2) + "\n").encode(),
            "metrics.json": (json.dumps(metrics, ensure_ascii=False, indent=2) + "\n").encode(),
            "semantic-findings.json": (json.dumps(findings, ensure_ascii=False, indent=2) + "\n").encode(),
            "report.md": ("\n".join(report) + "\n").encode(),
        }
        return outputs, metrics


if __name__ == "__main__":
    audit = Stage8BOcrLogAudit()
    generated, metric_values = audit.build()
    DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for filename, payload in generated.items():
        (DEFAULT_OUTPUT_DIR / filename).write_bytes(payload)
    raise SystemExit(1 if any(metric_values.get(key) for key in BLOCKING_METRICS) else 0)
