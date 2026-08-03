from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from dev_tools.stage8a_semantic_policy import (
    CJK_RE,
    CYRILLIC_RE,
    IMMUTABLE_STAGE8A_BASE_SHA,
    MOJIBAKE_RE,
    STAGE8A_SCOPE_FILES,
    STAGE8A_SCOPE_PREFIXES,
    classify_message,
    has_ordinary_english,
    placeholder_signature,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "artifacts" / "stage8a"
SCHEMA_VERSION = 1
BLOCKING_METRICS = (
    "stage8a_unresolved",
    "stage8a_cjk_first_party_remaining",
    "stage8a_english_first_party_remaining",
    "stage8a_placeholder_mismatches",
    "stage8a_severity_mismatches",
    "stage8a_sequence_mismatches",
    "stage8a_control_flow_mismatches",
    "stage8a_raw_payload_violations",
    "stage8a_binary_payload_log_findings",
    "stage8a_secret_findings",
    "stage8a_mojibake_findings",
)
COLUMNS = (
    "path",
    "stable_identifier",
    "function_owner",
    "call_kind",
    "ast_path",
    "message_fingerprint",
    "subsystem",
    "backend",
    "runtime_owner",
    "message_or_template",
    "classification",
    "stage_owner",
    "translation_required",
    "raw_external_payload",
    "user_actionable",
    "placeholder_signature",
    "severity",
    "evidence",
)
SECRET_PATTERNS = {
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github_token": re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b"),
    "authorization": re.compile(r"\bAuthorization:\s*(?:Bearer|Basic)\s+\S+", re.I),
    "credential_url": re.compile(r"https?://[^/\s:@]+:[^@\s/]+@"),
}


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def resolve_commit(root: Path, ref: str) -> str:
    result = _git(root, "rev-parse", "--verify", f"{ref}^{{commit}}", check=False)
    if result.returncode != 0:
        raise RuntimeError(f"Не удалось разрешить Stage 8A base ref: {ref}")
    return result.stdout.strip()


def head_sha(root: Path) -> str:
    return resolve_commit(root, "HEAD")


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parts: list[str] = []
        current: ast.AST = node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
        return ".".join(reversed(parts))
    return ""


@dataclass(frozen=True)
class StaticTemplate:
    text: str
    kind: str
    node: ast.AST


def _static_template(node: ast.AST) -> StaticTemplate | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return StaticTemplate(node.value, "constant", node)
    if isinstance(node, ast.JoinedStr):
        output: list[str] = []
        index = 0
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                output.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                output.append("{" + str(index) + "}")
                index += 1
            else:
                return None
        return StaticTemplate("".join(output), "fstring", node)
    if (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Mod)
        and isinstance(node.left, ast.Constant)
        and isinstance(node.left.value, str)
    ):
        return StaticTemplate(node.left.value, "percent", node)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
        and isinstance(node.func.value, ast.Constant)
        and isinstance(node.func.value.value, str)
    ):
        return StaticTemplate(node.func.value.value, "format", node)
    return None


def _message_nodes(node: ast.AST) -> Iterable[StaticTemplate]:
    template = _static_template(node)
    if template is not None:
        yield template
        return
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        yield from _message_nodes(node.left)
        yield from _message_nodes(node.right)


def _scope_files(root: Path) -> list[Path]:
    files = sorted((root / "module" / "device").rglob("*.py"))
    files.extend(root / path for path in STAGE8A_SCOPE_FILES if (root / path).is_file())
    return files


def _subsystem(path: str) -> str:
    if path == "module/webui/api.py":
        return "webui_live_preview_control"
    if "/platform/" in path:
        return "emulator_platform_lifecycle"
    if "/method/" in path:
        if "/scrcpy/" in path:
            return "scrcpy"
        return Path(path).stem
    if path.endswith("screenshot.py"):
        return "screenshot"
    if path.endswith(("control.py", "input.py")):
        return "control_input"
    if path.endswith(("connection.py", "connection_attr.py")):
        return "device_adb_connection"
    return "device"


def _backend(path: str, owner: str) -> str:
    names = (
        "ws-scrcpy", "scrcpy", "uiautomator2", "DroidCast", "aScreenCap",
        "NemuIpc", "LDOpenGL", "minitouch", "MaaTouch", "Hermit", "WSA",
        "ADB", "MuMu", "LDPlayer", "BlueStacks",
    )
    haystack = f"{path} {owner}".lower()
    for name in names:
        if name.lower().replace("-", "_") in haystack.replace("-", "_"):
            return name
    return "device"


def _severity(call_kind: str) -> str:
    if call_kind.startswith("logger."):
        return call_kind.split(".", 1)[1]
    if call_kind == "possible_reasons":
        return "actionable_reason"
    if call_kind.endswith(("Error", "Exception")):
        return "exception"
    return "runtime"


def _user_actionable(call_kind: str) -> bool:
    return call_kind in {
        "logger.warning", "logger.error", "logger.critical", "logger.exception",
        "possible_reasons",
    } or call_kind.endswith(("Error", "Exception"))


def _fingerprint(message: str) -> str:
    normalized = re.sub(r"\s+", " ", message.strip())
    normalized = re.sub(r"\{[^{}]*\}", "{}", normalized)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]


class RuntimeScanner:
    def __init__(self, path: str, source: str):
        self.path = path
        self.source = source
        self.tree = ast.parse(source, filename=path)
        self.rows: list[dict[str, Any]] = []
        self.node_keys: dict[str, tuple[int, int, int, int, str]] = {}

    def scan(self) -> list[dict[str, Any]]:
        self._visit(self.tree, "Module", ())
        self.rows.sort(key=lambda row: (row["ast_path"], row["arg_role"]))
        return self.rows

    def _visit(self, node: ast.AST, ast_path: str, owners: tuple[str, ...]) -> None:
        next_owners = owners
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            next_owners = (*owners, node.name)
        if isinstance(node, ast.Call):
            self._visit_call(node, ast_path, next_owners)
        for field, value in ast.iter_fields(node):
            if isinstance(value, ast.AST):
                self._visit(value, f"{ast_path}.{field}", next_owners)
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    if isinstance(item, ast.AST):
                        self._visit(item, f"{ast_path}.{field}[{index}]", next_owners)

    def _visit_call(self, node: ast.Call, ast_path: str, owners: tuple[str, ...]) -> None:
        name = _call_name(node.func)
        candidates: list[tuple[ast.AST, str]] = []
        if name.startswith("logger.") and node.args:
            candidates.append((node.args[0], "message"))
            if name == "logger.attr" and len(node.args) > 1:
                candidates.append((node.args[1], "value"))
            if name == "logger.error_context":
                candidates.extend(
                    (keyword.value, str(keyword.arg))
                    for keyword in node.keywords
                    if keyword.arg in {"title", "reason", "impact", "action"}
                )
        elif name == "possible_reasons":
            candidates.extend((arg, f"reason_{index}") for index, arg in enumerate(node.args))
        elif name.endswith(("Error", "Exception")) and node.args:
            candidates.append((node.args[0], "exception_message"))
        else:
            return

        owner = ".".join(owners)
        for role_index, (candidate, role) in enumerate(candidates):
            templates = list(_message_nodes(candidate))
            if not templates and name.startswith("logger.") and role == "message":
                self._add_dynamic(node, candidate, ast_path, owner, name, role)
                continue
            for message_index, template in enumerate(templates):
                self._add_static(
                    node=node,
                    template=template,
                    ast_path=f"{ast_path}:arg:{role_index}:part:{message_index}",
                    owner=owner,
                    call_kind=name,
                    role=role,
                )

    def _identifier(self, ast_path: str, owner: str, call_kind: str, role: str) -> str:
        payload = "\0".join((self.path, owner, call_kind, role, ast_path))
        return "stage8a:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]

    def _base_row(
        self,
        *,
        node: ast.Call,
        ast_path: str,
        owner: str,
        call_kind: str,
        role: str,
        message: str,
    ) -> dict[str, Any]:
        classification, stage_owner, required, evidence = classify_message(
            path=self.path,
            function_owner=owner,
            call_kind=call_kind,
            arg_role=role,
            message=message,
        )
        identifier = self._identifier(ast_path, owner, call_kind, role)
        return {
            "path": self.path,
            "stable_identifier": identifier,
            "function_owner": owner,
            "call_kind": call_kind,
            "ast_path": ast_path,
            "message_fingerprint": _fingerprint(message),
            "subsystem": _subsystem(self.path),
            "backend": _backend(self.path, owner),
            "runtime_owner": "Stage 8A device runtime",
            "message_or_template": message,
            "classification": classification,
            "stage_owner": stage_owner,
            "translation_required": required,
            "raw_external_payload": classification == "raw_external_payload",
            "user_actionable": _user_actionable(call_kind),
            "placeholder_signature": list(placeholder_signature(message)),
            "severity": _severity(call_kind),
            "evidence": evidence,
            "line": node.lineno,
            "arg_role": role,
        }

    def _add_static(
        self,
        *,
        node: ast.Call,
        template: StaticTemplate,
        ast_path: str,
        owner: str,
        call_kind: str,
        role: str,
    ) -> None:
        row = self._base_row(
            node=node,
            ast_path=ast_path,
            owner=owner,
            call_kind=call_kind,
            role=role,
            message=template.text,
        )
        key = (
            template.node.lineno,
            template.node.col_offset,
            template.node.end_lineno,
            template.node.end_col_offset,
            template.kind,
        )
        row["node_key"] = key
        self.node_keys[row["stable_identifier"]] = key
        self.rows.append(row)

    def _add_dynamic(
        self,
        node: ast.Call,
        candidate: ast.AST,
        ast_path: str,
        owner: str,
        call_kind: str,
        role: str,
    ) -> None:
        row = self._base_row(
            node=node,
            ast_path=f"{ast_path}:dynamic",
            owner=owner,
            call_kind=call_kind,
            role=role,
            message="<dynamic expression>",
        )
        row["raw_expression"] = ast.dump(candidate, include_attributes=False)
        row["node_key"] = None
        self.rows.append(row)


class _MessageNormalizer(ast.NodeTransformer):
    def __init__(self, keys: set[tuple[int, int, int, int, str]]):
        self.keys = keys

    @staticmethod
    def _key(node: ast.AST, kind: str) -> tuple[int, int, int, int, str]:
        return (node.lineno, node.col_offset, node.end_lineno, node.end_col_offset, kind)

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if isinstance(node.value, str) and self._key(node, "constant") in self.keys:
            return ast.copy_location(ast.Constant(value="<STAGE8A_MESSAGE>"), node)
        return node

    def visit_JoinedStr(self, node: ast.JoinedStr) -> ast.AST:
        if self._key(node, "fstring") in self.keys:
            values: list[ast.AST] = [ast.Constant(value="<LITERAL>")]
            for value in node.values:
                if isinstance(value, ast.FormattedValue):
                    values.append(self.visit(value))
                    values.append(ast.Constant(value="<LITERAL>"))
            replacement = ast.JoinedStr(values=values)
            return ast.copy_location(replacement, node)
        return self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        if (
            isinstance(node.op, ast.Mod)
            and self._key(node, "percent") in self.keys
            and isinstance(node.left, ast.Constant)
        ):
            node = self.generic_visit(node)
            node.left = ast.copy_location(ast.Constant(value="<STAGE8A_MESSAGE>"), node.left)
            return node
        return self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> ast.AST:
        if (
            self._key(node, "format") in self.keys
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Constant)
        ):
            node = self.generic_visit(node)
            node.func.value = ast.copy_location(
                ast.Constant(value="<STAGE8A_MESSAGE>"),
                node.func.value,
            )
            return node
        return self.generic_visit(node)


def normalized_ast(source: str, message_keys: set[tuple[int, int, int, int, str]]) -> str:
    tree = ast.parse(source)
    tree = _MessageNormalizer(message_keys).visit(tree)
    ast.fix_missing_locations(tree)
    return ast.dump(tree, include_attributes=False)


def _read_git_file(root: Path, commit: str, path: str) -> str:
    result = _git(root, "show", f"{commit}:{path}", check=False)
    if result.returncode != 0:
        raise RuntimeError(f"Не удалось прочитать {path} из baseline {commit}")
    return result.stdout


def _table(rows: list[dict[str, Any]]) -> bytes:
    return (
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "columns": list(COLUMNS),
                "entries": [[row.get(column) for column in COLUMNS] for row in rows],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _remaining_outside_stage8a(root: Path) -> int:
    total = 0
    excluded = ("tests/", "dev_tools/", ".venv/", "artifacts/")
    for file in root.rglob("*.py"):
        rel = file.relative_to(root).as_posix()
        if rel.startswith(excluded):
            continue
        if rel.startswith(STAGE8A_SCOPE_PREFIXES) or rel in STAGE8A_SCOPE_FILES:
            continue
        try:
            tree = ast.parse(file.read_text(encoding="utf-8"), filename=rel)
        except (OSError, UnicodeError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not _call_name(node.func).startswith("logger."):
                continue
            if not node.args:
                continue
            template = _static_template(node.args[0])
            if template is None:
                continue
            text = template.text
            if CJK_RE.search(text) or (not CYRILLIC_RE.search(text) and re.search(r"[A-Za-z]{2,}", text)):
                total += 1
    return total


class Stage8ADeviceLogAudit:
    def __init__(self, root: Path = ROOT, base_ref: str = IMMUTABLE_STAGE8A_BASE_SHA):
        self.root = root
        self.base_ref = base_ref
        self.base_sha = resolve_commit(root, base_ref)
        self.head_sha = head_sha(root)

    def build(self) -> tuple[dict[str, bytes], dict[str, Any]]:
        if self.base_sha == self.head_sha:
            raise RuntimeError(
                "Stage 8A migration verifier отказался от self-diff: "
                "HEAD совпадает с immutable pre-Stage-8A baseline."
            )

        head_rows: list[dict[str, Any]] = []
        base_rows: list[dict[str, Any]] = []
        head_sources: dict[str, str] = {}
        base_sources: dict[str, str] = {}
        head_scanners: dict[str, RuntimeScanner] = {}
        base_scanners: dict[str, RuntimeScanner] = {}

        for file in _scope_files(self.root):
            path = file.relative_to(self.root).as_posix()
            head_source = file.read_text(encoding="utf-8")
            base_source = _read_git_file(self.root, self.base_sha, path)
            head_scanner = RuntimeScanner(path, head_source)
            base_scanner = RuntimeScanner(path, base_source)
            head_rows.extend(head_scanner.scan())
            base_rows.extend(base_scanner.scan())
            head_sources[path] = head_source
            base_sources[path] = base_source
            head_scanners[path] = head_scanner
            base_scanners[path] = base_scanner

        head_lookup = {row["stable_identifier"]: row for row in head_rows}
        base_lookup = {row["stable_identifier"]: row for row in base_rows}
        findings: list[dict[str, Any]] = []

        missing = sorted(set(base_lookup) - set(head_lookup))
        added = sorted(set(head_lookup) - set(base_lookup))
        for identifier in missing:
            findings.append({"kind": "removed_runtime_message", "stable_identifier": identifier})
        for identifier in added:
            findings.append({"kind": "added_runtime_message", "stable_identifier": identifier})

        placeholder_mismatches = 0
        severity_mismatches = 0
        raw_payload_violations = 0
        approved_delta: list[dict[str, Any]] = []
        translated = 0

        for identifier in sorted(set(base_lookup) & set(head_lookup)):
            before = base_lookup[identifier]
            after = head_lookup[identifier]
            if before["placeholder_signature"] != after["placeholder_signature"]:
                placeholder_mismatches += 1
                findings.append({
                    "kind": "placeholder_mismatch",
                    "stable_identifier": identifier,
                    "before": before["placeholder_signature"],
                    "after": after["placeholder_signature"],
                })
            if before["severity"] != after["severity"] or before["call_kind"] != after["call_kind"]:
                severity_mismatches += 1
                findings.append({
                    "kind": "severity_mismatch",
                    "stable_identifier": identifier,
                    "before": before["call_kind"],
                    "after": after["call_kind"],
                })
            if before.get("raw_expression") != after.get("raw_expression"):
                raw_payload_violations += 1
                findings.append({
                    "kind": "raw_expression_changed",
                    "stable_identifier": identifier,
                })
            if before["message_or_template"] != after["message_or_template"]:
                approved_delta.append({
                    "path": after["path"],
                    "stable_identifier": identifier,
                    "function_owner": after["function_owner"],
                    "call_kind": after["call_kind"],
                    "before": before["message_or_template"],
                    "after": after["message_or_template"],
                })
            if before["translation_required"] and not after["translation_required"]:
                translated += 1

        base_sequence = [
            (row["path"], row["stable_identifier"], row["call_kind"], row["arg_role"])
            for row in base_rows
        ]
        head_sequence = [
            (row["path"], row["stable_identifier"], row["call_kind"], row["arg_role"])
            for row in head_rows
        ]
        sequence_mismatches = int(base_sequence != head_sequence)

        control_flow_mismatches = 0
        for path in sorted(head_sources):
            identifiers = {
                row["stable_identifier"]
                for row in base_rows
                if row["path"] == path
                and row["classification"] == "stage8a_first_party_message"
            }
            base_keys = {
                base_scanners[path].node_keys[identifier]
                for identifier in identifiers
                if identifier in base_scanners[path].node_keys
            }
            head_keys = {
                head_scanners[path].node_keys[identifier]
                for identifier in identifiers
                if identifier in head_scanners[path].node_keys
            }
            if normalized_ast(base_sources[path], base_keys) != normalized_ast(head_sources[path], head_keys):
                control_flow_mismatches += 1
                findings.append({"kind": "control_flow_mismatch", "path": path})

        unresolved_rows = [
            row
            for row in head_rows
            if row["stage_owner"] == "stage8a" and row["translation_required"]
        ]
        cjk_remaining = sum(
            1 for row in unresolved_rows if CJK_RE.search(row["message_or_template"])
        )
        english_remaining = sum(
            1
            for row in unresolved_rows
            if not CJK_RE.search(row["message_or_template"])
            and (
                not CYRILLIC_RE.search(row["message_or_template"])
                or has_ordinary_english(row["message_or_template"])
            )
        )
        secret_findings = sum(
            1
            for row in head_rows
            for pattern in SECRET_PATTERNS.values()
            if pattern.search(row["message_or_template"])
        )
        mojibake_findings = sum(
            1 for row in head_rows if MOJIBAKE_RE.search(row["message_or_template"])
        )

        metrics = {
            "stage8a_candidates_total": len(head_rows),
            "stage8a_translation_required_start": sum(
                1
                for row in base_rows
                if row["stage_owner"] == "stage8a" and row["translation_required"]
            ),
            "stage8a_translated": translated,
            "stage8a_reviewed_technical": sum(
                1
                for row in head_rows
                if row["classification"] in {"technical_identifier", "raw_external_payload"}
            ),
            "stage8a_raw_external": sum(
                1 for row in head_rows if row["raw_external_payload"]
            ),
            "stage8a_developer_only": 0,
            "stage8a_transferred_to_stage8b": sum(
                1 for row in head_rows if row["classification"] == "stage8b_ocr"
            ),
            "stage8a_transferred_to_stage8c": sum(
                1 for row in head_rows if row["classification"] == "stage8c_scheduler"
            ),
            "stage8a_transferred_to_stage8d": 0,
            "stage8a_transferred_to_stage8e": 0,
            "stage8a_unresolved": len(unresolved_rows) + len(missing) + len(added),
            "stage8a_cjk_first_party_remaining": cjk_remaining,
            "stage8a_english_first_party_remaining": english_remaining,
            "stage8a_placeholder_mismatches": placeholder_mismatches,
            "stage8a_severity_mismatches": severity_mismatches,
            "stage8a_sequence_mismatches": sequence_mismatches,
            "stage8a_control_flow_mismatches": control_flow_mismatches,
            "stage8a_raw_payload_violations": raw_payload_violations,
            "stage8a_binary_payload_log_findings": int(control_flow_mismatches > 0),
            "stage8a_secret_findings": secret_findings,
            "stage8a_mojibake_findings": mojibake_findings,
            "remaining_log_translation_count": _remaining_outside_stage8a(self.root),
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
        }

        coverage = [
            {
                "backend": backend,
                "platform": platform,
                "screenshot_control": mode,
                "runtime_owner": "Stage 8A device runtime",
                "available_in_ci": False,
                "covered_fixture": True,
                "actual_user_backend": False,
                "acceptance_required": backend in {"ADB", "scrcpy", "uiautomator2"},
                "level": "CI_FIXTURE",
                "limitations": "Реальное устройство подтверждается отдельным acceptance-pass.",
            }
            for backend, platform, mode in (
                ("ADB", "Android/Windows", "screenshot/control"),
                ("uiautomator2", "Android", "screenshot/control"),
                ("scrcpy", "Android", "screenshot/control"),
                ("DroidCast", "Android", "screenshot"),
                ("aScreenCap", "Android", "screenshot"),
                ("NemuIpc", "Windows/MuMu", "screenshot/control"),
                ("LDOpenGL", "Windows/LDPlayer", "screenshot"),
                ("minitouch", "Android", "control"),
                ("MaaTouch", "Android", "control"),
                ("Hermit", "Android", "control"),
                ("WSA", "Windows", "device"),
            )
        ]

        contract = {
            "schema_version": SCHEMA_VERSION,
            "immutable_base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "translation_only": control_flow_mismatches == 0,
            "logger_sequence_preserved": sequence_mismatches == 0,
            "placeholder_contract_preserved": placeholder_mismatches == 0,
            "raw_expression_contract_preserved": raw_payload_violations == 0,
            "backend_coverage": coverage,
        }
        status = "PASS" if not any(metrics[key] for key in BLOCKING_METRICS) else "FAIL"
        report = [
            "# Stage 8A — device/ADB/runtime logs",
            "",
            f"Статус: **{status}**",
            "",
            f"- immutable base: `{self.base_sha}`",
            f"- head: `{self.head_sha}`",
            f"- candidates: {metrics['stage8a_candidates_total']}",
            f"- required at start: {metrics['stage8a_translation_required_start']}",
            f"- translated: {metrics['stage8a_translated']}",
            f"- unresolved: {metrics['stage8a_unresolved']}",
            f"- transferred Stage 8B: {metrics['stage8a_transferred_to_stage8b']}",
            f"- transferred Stage 8C: {metrics['stage8a_transferred_to_stage8c']}",
            f"- remaining later-stage logs: {metrics['remaining_log_translation_count']}",
            "",
            "## Blocking metrics",
            "",
            *[f"- {key}: {metrics[key]}" for key in BLOCKING_METRICS],
            "",
        ]
        outputs = {
            "scope.json": _table(head_rows),
            "metrics.json": _json_bytes(metrics),
            "report.md": ("\n".join(report) + "\n").encode("utf-8"),
            "semantic-findings.json": _json_bytes(findings),
            "approved-delta.json": _json_bytes(approved_delta),
            "contract.json": _json_bytes(contract),
        }
        return outputs, metrics
