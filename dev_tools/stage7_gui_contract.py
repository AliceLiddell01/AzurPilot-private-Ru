from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import re
import subprocess
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from dev_tools.russianization_audit import json_bytes


ROOT = Path(__file__).resolve().parents[1]
CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff]")
CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
LATIN_RE = re.compile(r"[A-Za-z]{2,}")
BRACE_PLACEHOLDER_RE = re.compile(r"\{(?:…|[^{}]*)\}")
PERCENT_PLACEHOLDER_RE = re.compile(
    r"%\([^)]+\)[#0 +\-]?(?:\d+|\*)?(?:\.\d+)?[diouxXeEfFgGcrs]"
    r"|%[#0+\-]?(?:\d+|\*)?(?:\.\d+)?[diouxXeEfFgGcrs%]"
)
LOGGER_METHODS = {
    "debug",
    "info",
    "warning",
    "error",
    "critical",
    "exception",
    "exception_context",
    "error_context",
    "hr",
    "attr",
}
CONTEXT_FIELDS = {"title", "reason", "impact", "action"}
TECHNICAL_IDENTIFIERS = {
    "SSL",
    "PID",
    "IPv4",
    "IPv6",
    "Electron",
    "WebUI",
    "uv",
    "taskkill",
    "psutil",
}
GUI_BLOCKING_METRICS = (
    "stage7_gui_unresolved",
    "stage7_gui_cjk_first_party_remaining",
    "stage7_gui_english_first_party_remaining",
    "stage7_gui_control_flow_mismatches",
    "stage7_gui_placeholder_mismatches",
    "stage7_gui_severity_mismatches",
    "stage7_gui_sequence_mismatches",
)


@dataclass(frozen=True)
class GuiLiteral:
    semantic_identifier: str
    owner: str
    call_kind: str
    role: str
    ast_path: str
    source: str
    template: str
    placeholder_signature: tuple[str, ...]
    classification: str
    translation_required: bool
    raw_external: bool
    evidence: str


@dataclass(frozen=True)
class GuiCallSignature:
    semantic_identifier: str
    owner: str
    call_kind: str
    ast_path: str
    non_message_shape: str
    roles: tuple[str, ...]


def _git_show(root: Path, revision: str, path: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "show", f"{revision}:{path}"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout


def _path_string(path: tuple[str | int, ...]) -> str:
    return "/".join(str(part) for part in path)


def _walk(node: ast.AST, path: tuple[str | int, ...] = ()):
    yield path, node
    for field, value in ast.iter_fields(node):
        if isinstance(value, ast.AST):
            yield from _walk(value, (*path, field))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, ast.AST):
                    yield from _walk(item, (*path, field, index))


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    result: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            result[child] = parent
    return result


def _function_owner(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name
    return "<module>"


def _logger_method(node: ast.Call) -> str | None:
    func = node.func
    if not isinstance(func, ast.Attribute):
        return None
    if not isinstance(func.value, ast.Name) or func.value.id != "logger":
        return None
    if func.attr not in LOGGER_METHODS:
        return None
    return func.attr


def _literal_template(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                expression = ast.unparse(value.value)
                conversion = "" if value.conversion == -1 else f"!{chr(value.conversion)}"
                format_spec = ""
                if value.format_spec is not None:
                    format_spec = f":{ast.unparse(value.format_spec)}"
                parts.append(f"{{{expression}{conversion}{format_spec}}}")
            else:
                return None
        return "".join(parts)
    return None


def _placeholder_signature(node: ast.AST) -> tuple[str, ...]:
    result: list[str] = []
    template = _literal_template(node)
    if template is not None:
        result.extend(BRACE_PLACEHOLDER_RE.findall(template))
        result.extend(PERCENT_PLACEHOLDER_RE.findall(template))
    if isinstance(node, ast.JoinedStr):
        result = []
        for value in node.values:
            if isinstance(value, ast.FormattedValue):
                result.append(
                    "f:" + ast.dump(value.value, include_attributes=False)
                    + f":conversion={value.conversion}:format="
                    + (
                        ast.dump(value.format_spec, include_attributes=False)
                        if value.format_spec is not None
                        else ""
                    )
                )
            elif isinstance(value, ast.Constant) and isinstance(value.value, str):
                result.extend(PERCENT_PLACEHOLDER_RE.findall(value.value))
    return tuple(result)


def _classification(template: str) -> tuple[str, bool, str]:
    stripped = template.strip()
    if stripped in TECHNICAL_IDENTIFIERS:
        return (
            "technical_identifier",
            False,
            "Точный product/protocol token сохраняется без перевода.",
        )
    if CJK_RE.search(template):
        return (
            "stage7_first_party_message",
            True,
            "First-party CJK-текст текущего WebUI supervisor требует русификации.",
        )
    if CYRILLIC_RE.search(template):
        return (
            "stage7_first_party_message",
            False,
            "First-party контекст на русском языке.",
        )
    if LATIN_RE.search(template):
        return (
            "stage7_first_party_message",
            True,
            "Обычный английский first-party текст требует русификации.",
        )
    return (
        "technical_identifier",
        False,
        "Структурный символ или нейтральный технический фрагмент.",
    )


def _raw_external(node: ast.AST) -> bool:
    names = {
        child.id
        for child in ast.walk(node)
        if isinstance(child, ast.Name)
    }
    attributes = {
        ast.unparse(child)
        for child in ast.walk(node)
        if isinstance(child, ast.Attribute)
    }
    calls = {
        ast.unparse(child.func)
        for child in ast.walk(node)
        if isinstance(child, ast.Call)
    }
    return bool(
        names.intersection({"exc", "error", "child_pids", "response"})
        or "result.returncode" in attributes
        or "redact_sensitive_text" in calls
    )


def _non_message_shape(call: ast.Call, allowed_nodes: set[int]) -> str:
    clone = copy.deepcopy(call)

    class Scrubber(ast.NodeTransformer):
        def visit_Constant(self, node: ast.Constant):  # noqa: N802
            if isinstance(node.value, str):
                return ast.copy_location(ast.Constant("<STRING>"), node)
            return node

    return ast.dump(Scrubber().visit(clone), include_attributes=False)


def _structural_path(path: tuple[str | int, ...]) -> str:
    """Return a line/order-independent AST field path.

    List indexes are deliberately omitted.  The occurrence counter is scoped to
    the remaining structural fingerprint, so inserting an unrelated statement
    does not rename existing inventory entries.
    """
    return "/".join(str(part) for part in path if not isinstance(part, int))


def _semantic_identifier(
    owner: str,
    call_kind: str,
    call_path: tuple[str | int, ...],
    role: str,
    placeholder_signature: tuple[str, ...],
    non_message_shape: str,
    occurrence: int,
) -> str:
    payload = json.dumps(
        {
            "owner": owner,
            "call_kind": call_kind,
            "structural_ast_path": _structural_path(call_path),
            "role": role,
            "non_message_shape": non_message_shape,
            "placeholder_signature": placeholder_signature,
            "occurrence": occurrence,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "gui:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _dynamic_literal_specs(
    tree: ast.AST,
    path_lookup: dict[ast.AST, tuple[str | int, ...]],
    parents: dict[ast.AST, ast.AST],
) -> list[tuple[ast.AST, str, str, str, tuple[str | int, ...]]]:
    specs: list[tuple[ast.AST, str, str, str, tuple[str | int, ...]]] = []
    for path, node in _walk(tree):
        if not isinstance(node, ast.Call):
            continue
        owner = _function_owner(node, parents)
        if (
            owner == "_stop_dependency_sync_service_tree"
            and isinstance(node.func, ast.Name)
            and node.func.id == "_stop_process_tree"
            and len(node.args) >= 2
            and _literal_template(node.args[1]) is not None
        ):
            specs.append((node.args[1], owner, "dynamic_label", "process_name", path))
        if (
            owner == "_stop_process_tree"
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 3
            and _literal_template(node.args[2]) is not None
            and ast.unparse(node.args[1]) in {'"pid"', "'pid'"}
        ):
            specs.append((node.args[2], owner, "dynamic_label", "fallback", path))
        if (
            owner == "_sync_dependencies"
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "result"
            and node.func.attr == "get"
            and len(node.args) >= 2
            and _literal_template(node.args[0]) == "error"
            and _literal_template(node.args[1]) is not None
        ):
            specs.append((node.args[1], owner, "dynamic_label", "fallback", path))
    return specs


def extract_gui_inventory(source: str) -> tuple[list[GuiLiteral], list[GuiCallSignature]]:
    tree = ast.parse(source)
    paths = {node: path for path, node in _walk(tree)}
    parents = _parent_map(tree)
    literals: list[GuiLiteral] = []
    calls: list[GuiCallSignature] = []
    identifier_counters: Counter[tuple[Any, ...]] = Counter()

    def allocate_identifier(
        *,
        owner: str,
        call_kind: str,
        call_path: tuple[str | int, ...],
        role: str,
        placeholder_signature: tuple[str, ...],
        non_message_shape: str,
    ) -> str:
        key = (
            owner,
            call_kind,
            _structural_path(call_path),
            role,
            placeholder_signature,
            non_message_shape,
        )
        identifier_counters[key] += 1
        return _semantic_identifier(
            owner,
            call_kind,
            call_path,
            role,
            placeholder_signature,
            non_message_shape,
            identifier_counters[key],
        )

    for call_path, node in _walk(tree):
        if not isinstance(node, ast.Call):
            continue
        method = _logger_method(node)
        if method is None:
            continue
        owner = _function_owner(node, parents)
        call_shape = _non_message_shape(node, set())
        specs: list[tuple[str, ast.AST]] = []
        if node.args and _literal_template(node.args[0]) is not None:
            specs.append(("message", node.args[0]))
        if method in {"exception_context", "error_context"}:
            for keyword in node.keywords:
                if keyword.arg in CONTEXT_FIELDS and _literal_template(keyword.value) is not None:
                    specs.append((str(keyword.arg), keyword.value))

        call_ids: list[str] = []
        for role, literal_node in specs:
            template = _literal_template(literal_node)
            assert template is not None
            signature = _placeholder_signature(literal_node)
            identifier = allocate_identifier(
                owner=owner,
                call_kind=f"logger.{method}",
                call_path=call_path,
                role=role,
                placeholder_signature=signature,
                non_message_shape=call_shape,
            )
            classification, required, evidence = _classification(template)
            literals.append(
                GuiLiteral(
                    semantic_identifier=identifier,
                    owner=owner,
                    call_kind=f"logger.{method}",
                    role=role,
                    ast_path=_path_string(paths[literal_node]),
                    source=ast.get_source_segment(source, literal_node) or "",
                    template=template,
                    placeholder_signature=signature,
                    classification=classification,
                    translation_required=required,
                    raw_external=_raw_external(literal_node),
                    evidence=evidence,
                )
            )
            call_ids.append(identifier)

        call_identifier = allocate_identifier(
            owner=owner,
            call_kind=f"logger.{method}",
            call_path=call_path,
            role="call",
            placeholder_signature=tuple(call_ids),
            non_message_shape=call_shape,
        )
        calls.append(
            GuiCallSignature(
                semantic_identifier=call_identifier,
                owner=owner,
                call_kind=f"logger.{method}",
                ast_path=_path_string(call_path),
                non_message_shape=call_shape,
                roles=tuple(role for role, _ in specs),
            )
        )

    for literal_node, owner, call_kind, role, call_path in _dynamic_literal_specs(
        tree, paths, parents
    ):
        template = _literal_template(literal_node)
        assert template is not None
        signature = _placeholder_signature(literal_node)
        dynamic_parent = parents[literal_node]
        if isinstance(dynamic_parent, ast.Call):
            dynamic_shape = _non_message_shape(dynamic_parent, set())
        else:
            dynamic_shape = ast.dump(dynamic_parent, include_attributes=False)
        identifier = allocate_identifier(
            owner=owner,
            call_kind=call_kind,
            call_path=call_path,
            role=role,
            placeholder_signature=signature,
            non_message_shape=dynamic_shape,
        )
        classification, required, evidence = _classification(template)
        literals.append(
            GuiLiteral(
                semantic_identifier=identifier,
                owner=owner,
                call_kind=call_kind,
                role=role,
                ast_path=_path_string(paths[literal_node]),
                source=ast.get_source_segment(source, literal_node) or "",
                template=template,
                placeholder_signature=signature,
                classification=classification,
                translation_required=required,
                raw_external=False,
                evidence="First-party динамическая подпись, отображаемая в журналах. " + evidence,
            )
        )

    literals.sort(key=lambda item: item.ast_path)
    calls.sort(key=lambda item: item.ast_path)
    return literals, calls


def _normalize_allowed_paths(
    source: str,
    allowed_paths: set[str],
) -> str:
    tree = ast.parse(source)
    path_lookup = {node: path for path, node in _walk(tree)}

    class Normalizer(ast.NodeTransformer):
        def generic_visit(self, node: ast.AST):
            path = _path_string(path_lookup[node])
            if path in allowed_paths:
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    return ast.copy_location(ast.Constant("<STAGE7_TRANSLATED_LITERAL>"), node)
                if isinstance(node, ast.JoinedStr):
                    values: list[ast.AST] = []
                    for value in node.values:
                        if isinstance(value, ast.Constant) and isinstance(value.value, str):
                            values.append(
                                ast.copy_location(
                                    ast.Constant("<STAGE7_TRANSLATED_LITERAL>"), value
                                )
                            )
                        else:
                            values.append(self.visit(value))
                    node.values = values
                    return node
            return super().generic_visit(node)

    normalized = Normalizer().visit(tree)
    ast.fix_missing_locations(normalized)
    return ast.dump(normalized, include_attributes=False)


def analyze_gui_contract(base_source: str, head_source: str, *, base_sha: str) -> dict[str, Any]:
    base_literals, base_calls = extract_gui_inventory(base_source)
    head_literals, head_calls = extract_gui_inventory(head_source)
    base_lookup = {item.semantic_identifier: item for item in base_literals}
    head_lookup = {item.semantic_identifier: item for item in head_literals}

    sequence_errors = sorted(set(base_lookup) ^ set(head_lookup))
    call_sequence_errors = []
    base_call_keys = [(item.owner, item.call_kind, item.ast_path) for item in base_calls]
    head_call_keys = [(item.owner, item.call_kind, item.ast_path) for item in head_calls]
    if base_call_keys != head_call_keys:
        call_sequence_errors.append(
            {"base": base_call_keys, "head": head_call_keys}
        )

    approved: list[dict[str, Any]] = []
    placeholder_errors: list[dict[str, Any]] = []
    unresolved = 0
    cjk_remaining = 0
    english_remaining = 0
    translated = 0
    technical = 0
    raw_external = 0
    allowed_paths: set[str] = set()

    for identifier in sorted(set(base_lookup) & set(head_lookup)):
        old = base_lookup[identifier]
        new = head_lookup[identifier]
        if old.placeholder_signature != new.placeholder_signature:
            placeholder_errors.append(
                {
                    "semantic_identifier": identifier,
                    "base": old.placeholder_signature,
                    "head": new.placeholder_signature,
                }
            )
        if old.translation_required:
            allowed_paths.add(old.ast_path)
            if new.translation_required:
                unresolved += 1
                if CJK_RE.search(new.template):
                    cjk_remaining += 1
                elif LATIN_RE.search(new.template) and not CYRILLIC_RE.search(new.template):
                    english_remaining += 1
            else:
                translated += 1
            approved.append(
                {
                    "semantic_identifier": identifier,
                    "owner": old.owner,
                    "call_kind": old.call_kind,
                    "role": old.role,
                    "ast_path": old.ast_path,
                    "base_literal": old.template,
                    "head_literal": new.template,
                    "placeholder_signature": list(old.placeholder_signature),
                    "raw_external": old.raw_external,
                }
            )
        else:
            if old.template != new.template:
                sequence_errors.append(
                    {
                        "semantic_identifier": identifier,
                        "unexpected_literal_change": [old.template, new.template],
                    }
                )
            if new.classification == "technical_identifier":
                technical += 1
        if new.raw_external:
            raw_external += 1

    normalized_base = _normalize_allowed_paths(base_source, allowed_paths)
    normalized_head = _normalize_allowed_paths(head_source, allowed_paths)
    control_flow_mismatches = int(normalized_base != normalized_head)

    severity_mismatches = int(
        [item.call_kind for item in base_calls] != [item.call_kind for item in head_calls]
    )
    sequence_mismatches = len(sequence_errors) + len(call_sequence_errors)

    inventory = [
        {
            **asdict(item),
            "placeholder_signature": list(item.placeholder_signature),
        }
        for item in head_literals
    ]
    metrics = {
        "stage7_gui_candidates": len(head_literals),
        "stage7_gui_translated": translated,
        "stage7_gui_reviewed_technical": technical,
        "stage7_gui_raw_external": raw_external,
        "stage7_gui_unresolved": unresolved,
        "stage7_gui_cjk_first_party_remaining": cjk_remaining,
        "stage7_gui_english_first_party_remaining": english_remaining,
        "stage7_gui_control_flow_mismatches": control_flow_mismatches,
        "stage7_gui_placeholder_mismatches": len(placeholder_errors),
        "stage7_gui_severity_mismatches": severity_mismatches,
        "stage7_gui_sequence_mismatches": sequence_mismatches,
    }
    errors = [
        f"{key}: {value}"
        for key, value in metrics.items()
        if key in GUI_BLOCKING_METRICS and value
    ]
    result = {
        "schema_version": 1,
        "base_sha": base_sha,
        "metrics": metrics,
        "errors": errors,
        "inventory": inventory,
        "approved_delta": approved,
        "placeholder_errors": placeholder_errors,
        "sequence_errors": sequence_errors,
        "call_sequence_errors": call_sequence_errors,
        "normalized_ast_sha256": {
            "base": hashlib.sha256(normalized_base.encode("utf-8")).hexdigest(),
            "head": hashlib.sha256(normalized_head.encode("utf-8")).hexdigest(),
        },
        "logger_signature_sha256": {
            "base": hashlib.sha256(
                json.dumps([asdict(item) for item in base_calls], sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "head": hashlib.sha256(
                json.dumps([asdict(item) for item in head_calls], sort_keys=True).encode("utf-8")
            ).hexdigest(),
        },
    }
    return result


def build_gui_contract(root: Path, base_sha: str) -> tuple[dict[str, bytes], dict[str, Any], list[str]]:
    base_source = _git_show(root, base_sha, "gui.py")
    head_source = (root / "gui.py").read_text(encoding="utf-8")
    result = analyze_gui_contract(base_source, head_source, base_sha=base_sha)
    metrics = dict(result["metrics"])
    outputs = {
        "gui-inventory.json": json_bytes(
            {
                "schema_version": result["schema_version"],
                "base_sha": base_sha,
                "entries": result["inventory"],
            }
        ),
        "gui-contract.json": json_bytes(
            {
                key: value
                for key, value in result.items()
                if key not in {"inventory", "approved_delta"}
            }
        ),
        "gui-approved-delta.json": json_bytes(
            {
                "schema_version": result["schema_version"],
                "base_sha": base_sha,
                "entries": result["approved_delta"],
            }
        ),
    }
    return outputs, metrics, list(result["errors"])


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверка translation-only diff gui.py")
    parser.add_argument("--base-ref", required=True)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(list(argv) if argv is not None else None)
    base_sha = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--verify", args.base_ref],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    outputs, metrics, errors = build_gui_contract(ROOT, base_sha)
    if args.output_dir is not None:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        for name, payload in outputs.items():
            (args.output_dir / name).write_bytes(payload)
    if errors:
        for error in errors:
            print(f"FAIL: {error}")
        return 1
    print(json.dumps(metrics, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
