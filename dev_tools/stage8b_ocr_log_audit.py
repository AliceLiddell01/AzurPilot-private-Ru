from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from dev_tools.stage8b_semantic_policy import (
    BLOCKING_METRICS,
    DEFAULT_OUTPUT_DIR,
    IMMUTABLE_STAGE8B_BASE_SHA,
    OCR_SCOPE_PATHS,
    PRESERVED_IDENTIFIERS,
    ROOT,
    TRANSLATION_ONLY_RUNTIME_PATHS,
)

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
LATIN_WORD_RE = re.compile(r"[A-Za-z]{2,}")
PLACEHOLDER_RE = re.compile(r"\{[^{}]+\}|%[-+#0-9.]*[a-zA-Z]")
MOJIBAKE_RE = re.compile(r"(?:Ã.|Â.|Ð.|Ñ.)")
ENGLISH_HINTS = frozenset(
    {
        "actual", "accuracy", "available", "closed", "created", "dataset",
        "detected", "error", "expected", "failed", "failure", "fast", "found",
        "image", "inference", "invalid", "loaded", "medium", "missing", "model",
        "provider", "rating", "result", "slow", "status", "unsupported", "using",
        "warning", "unable", "required", "requested", "range", "shape", "channels",
        "package", "connection", "server", "success", "disconnect",
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
        return ".".join((*self.class_stack, *self.function_stack)) or "<module>"

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

    def _append(self, call_kind: str, severity: str, template: str) -> None:
        owner = self._owner()
        classification = classify_message(template)
        required = translation_required(template)
        identifier_source = "|".join((self.path, owner, call_kind, severity, template))
        self.entries.append(
            ScopeEntry(
                path=self.path,
                stable_identifier=hashlib.sha256(
                    identifier_source.encode("utf-8")
                ).hexdigest()[:20],
                function_owner=owner,
                call_kind=call_kind,
                severity=severity,
                subsystem=subsystem_for_path(self.path),
                backend=backend_for_message(self.path, template),
                model=model_for_message(template),
                runtime_owner=self.path,
                message_or_template=template,
                classification=classification,
                stage_owner="8B",
                translation_required=required,
                raw_external_payload=has_raw_external_payload(template),
                recognized_value_payload=has_recognized_value(template),
                user_actionable=is_user_actionable(template),
                placeholder_signature=tuple(PLACEHOLDER_RE.findall(template)),
                evidence="active_runtime_source",
            )
        )

    def visit_Call(self, node: ast.Call) -> Any:
        call = self._call_name(node)
        if call and node.args:
            template = self._template(node.args[0])
            if template:
                self._append(call[0], call[1], template)
        self.generic_visit(node)

    def visit_Raise(self, node: ast.Raise) -> Any:
        exc = node.exc
        if isinstance(exc, ast.Call) and exc.args:
            template = self._template(exc.args[0])
            if template:
                self._append("raise", "exception", template)
        self.generic_visit(node)


def subsystem_for_path(path: str) -> str:
    if path.endswith("ocr_benchmark.py"):
        return "benchmark"
    if path.endswith("rpc.py") or path.endswith("stage8b_rpc_security.py"):
        return "rpc"
    if path.endswith("windows_ml.py"):
        return "windows_ml"
    if path.endswith("ncnn_ocr.py"):
        return "ncnn"
    return "ocr"


def backend_for_message(path: str, message: str) -> str:
    value = (path + " " + message).lower()
    if "windows_ml" in value or "windows ml" in value:
        return "windows_ml"
    if "ncnn" in value:
        return "ncnn"
    if "rpc" in value or "сервер" in value:
        return "rpc"
    if "onnx" in value or "provider" in value:
        return "onnx"
    return "all"


def model_for_message(message: str) -> str:
    for name in (
        "azur_lane_jp", "azur_lane", "ppocr_v6", "alocr_en_900k", "cn", "jp", "tw",
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
    text = PLACEHOLDER_RE.sub(" ", message)
    for identifier in sorted(PRESERVED_IDENTIFIERS, key=len, reverse=True):
        text = re.sub(re.escape(identifier), " ", text, flags=re.IGNORECASE)
    words = {word.lower() for word in LATIN_WORD_RE.findall(text)}
    ordinary = words - {"verbose", "azurpilot", "fallback", "backend", "benchmark"}
    return len(ordinary) >= 2 and bool(ordinary & ENGLISH_HINTS)


def translation_required(message: str) -> bool:
    return bool(CJK_RE.search(message) or _ordinary_english(message))


def classify_message(message: str) -> str:
    if has_recognized_value(message):
        return "recognized_value"
    if has_raw_external_payload(message):
        return "raw_external_payload"
    return "stage8b_first_party_message"


def _git_show(base: str, path: str) -> str:
    completed = subprocess.run(
        ["git", "show", f"{base}:{path}"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return completed.stdout if completed.returncode == 0 else ""


def _entries_from_source(path: str, source: str) -> list[ScopeEntry]:
    visitor = RuntimeStringVisitor(path)
    visitor.visit(ast.parse(source, filename=path))
    return visitor.entries


def collect_entries(root: Path = ROOT) -> list[ScopeEntry]:
    entries: list[ScopeEntry] = []
    for relative in OCR_SCOPE_PATHS:
        path = root / relative
        if not path.is_file():
            raise RuntimeError(f"Stage 8B scope path отсутствует: {relative}")
        try:
            entries.extend(_entries_from_source(relative, path.read_text(encoding="utf-8")))
        except SyntaxError as exc:
            raise RuntimeError(f"Не удалось разобрать {relative}: {exc}") from exc
    return entries


def _base_placeholder_map(base_sha: str) -> dict[tuple[str, str, str, str], list[tuple[str, ...]]]:
    mapping: dict[tuple[str, str, str, str], list[tuple[str, ...]]] = {}
    for relative in TRANSLATION_ONLY_RUNTIME_PATHS:
        source = _git_show(base_sha, relative)
        if not source:
            continue
        for entry in _entries_from_source(relative, source):
            key = (entry.path, entry.function_owner, entry.call_kind, entry.severity)
            mapping.setdefault(key, []).append(entry.placeholder_signature)
    return mapping


def _remaining_outside_scope(root: Path) -> int:
    scoped = set(OCR_SCOPE_PATHS)
    remaining = 0
    for path in root.rglob("*.py"):
        relative = path.relative_to(root).as_posix()
        if relative in scoped or any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if CJK_RE.search(source):
            remaining += 1
    return remaining


class Stage8BOcrLogAudit:
    def __init__(self, root: Path = ROOT, base_ref: str = IMMUTABLE_STAGE8B_BASE_SHA):
        if base_ref != IMMUTABLE_STAGE8B_BASE_SHA:
            raise RuntimeError("Immutable Stage 8B baseline изменён без policy review.")
        self.root = root
        self.base_sha = base_ref

    def build(self) -> tuple[dict[str, bytes], dict[str, Any]]:
        entries = collect_entries(self.root)
        unresolved = [entry for entry in entries if entry.translation_required]
        cjk = [entry for entry in unresolved if CJK_RE.search(entry.message_or_template)]
        english = [entry for entry in unresolved if _ordinary_english(entry.message_or_template)]
        mojibake = [entry for entry in entries if MOJIBAKE_RE.search(entry.message_or_template)]

        base_placeholders = _base_placeholder_map(self.base_sha)
        placeholder_mismatches: list[dict[str, Any]] = []
        for entry in entries:
            if entry.path not in TRANSLATION_ONLY_RUNTIME_PATHS:
                continue
            key = (entry.path, entry.function_owner, entry.call_kind, entry.severity)
            signatures = base_placeholders.get(key)
            if signatures and entry.placeholder_signature not in signatures:
                placeholder_mismatches.append(
                    {
                        "path": entry.path,
                        "owner": entry.function_owner,
                        "message": entry.message_or_template,
                        "head": entry.placeholder_signature,
                        "base_candidates": signatures,
                    }
                )

        metrics: dict[str, Any] = {
            "stage8b_candidates_total": len(entries),
            "stage8b_translation_required_start": len(entries),
            "stage8b_translated": len(entries) - len(unresolved),
            "stage8b_reviewed_technical": 0,
            "stage8b_recognized_value_payloads": sum(
                entry.recognized_value_payload for entry in entries
            ),
            "stage8b_raw_external": sum(entry.raw_external_payload for entry in entries),
            "stage8b_unresolved": len(unresolved),
            "stage8b_cjk_first_party_remaining": len(cjk),
            "stage8b_english_first_party_remaining": len(english),
            "stage8b_placeholder_mismatches": len(placeholder_mismatches),
            "stage8b_mojibake_findings": len(mojibake),
            "remaining_log_translation_count": _remaining_outside_scope(self.root),
        }
        findings = [
            {
                "kind": "untranslated_first_party",
                "path": entry.path,
                "owner": entry.function_owner,
                "message": entry.message_or_template,
                "stable_identifier": entry.stable_identifier,
            }
            for entry in unresolved
        ] + [
            {"kind": "placeholder_mismatch", **finding}
            for finding in placeholder_mismatches
        ]
        status = "FAIL" if any(metrics.get(key) for key in BLOCKING_METRICS) else "PASS"
        report = [
            "# Stage 8B OCR audit",
            "",
            f"Статус: **{status}**",
            f"Immutable base: `{self.base_sha}`",
            "",
            "## Метрики",
            *[f"- {key}: {value}" for key, value in sorted(metrics.items())],
        ]
        outputs = {
            "scope.json": (
                json.dumps([asdict(entry) for entry in entries], ensure_ascii=False, indent=2)
                + "\n"
            ).encode(),
            "metrics.json": (json.dumps(metrics, ensure_ascii=False, indent=2) + "\n").encode(),
            "semantic-findings.json": (
                json.dumps(findings, ensure_ascii=False, indent=2) + "\n"
            ).encode(),
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
