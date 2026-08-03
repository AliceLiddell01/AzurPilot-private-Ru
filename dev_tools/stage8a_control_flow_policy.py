from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

from dev_tools.stage8a_device_log_audit import (
    BLOCKING_METRICS,
    RuntimeScanner,
    _MessageNormalizer,
    _read_git_file,
)


APPROVED_METADATA_EXPRESSION_POLICY: dict[str, tuple[str, ...]] = {
    "module/device/method/adb.py": (
        "stage8a:02a86a9865dbc1093e09",
        "stage8a:6ca38ab496a16161333d",
        "stage8a:be70acc24d067058c769",
        "stage8a:e708c628015dd29175f8",
    ),
    "module/device/method/ascreencap.py": (
        "stage8a:08606827bbe10716867b",
        "stage8a:250124cad01d49f8406c",
        "stage8a:c47c575b0e093cf97900",
        "stage8a:d8cb9c57cd31e0b02046",
    ),
    "module/device/method/droidcast.py": (
        "stage8a:a01ed9cf599da4fb8d66",
        "stage8a:bb2843ad72740e9a5a93",
        "stage8a:7c450f67182a3eb815d1",
    ),
}

NodeKey = tuple[int, int, int, int, str]


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _node_key(node: ast.AST, kind: str) -> NodeKey:
    return (
        node.lineno,
        node.col_offset,
        node.end_lineno,
        node.end_col_offset,
        kind,
    )


def _find_fstring(tree: ast.AST, key: NodeKey) -> ast.JoinedStr | None:
    if key[-1] != "fstring":
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr) and _node_key(node, "fstring") == key:
            return node
    return None


def _single_formatted_value(node: ast.JoinedStr) -> ast.FormattedValue | None:
    values = [value for value in node.values if isinstance(value, ast.FormattedValue)]
    return values[0] if len(values) == 1 else None


def _is_exact_len_wrapper(before: ast.AST, after: ast.AST) -> bool:
    return (
        isinstance(after, ast.Call)
        and isinstance(after.func, ast.Name)
        and after.func.id == "len"
        and len(after.args) == 1
        and not after.keywords
        and ast.dump(after.args[0], include_attributes=False)
        == ast.dump(before, include_attributes=False)
    )


def _validate_metadata_expression_change(
    before: ast.JoinedStr,
    after: ast.JoinedStr,
) -> str | None:
    before_value = _single_formatted_value(before)
    after_value = _single_formatted_value(after)
    if before_value is None or after_value is None:
        return "Ожидался f-string ровно с одним форматируемым выражением."
    if before_value.conversion not in {-1, ord("r")} or after_value.conversion != -1:
        return "Допустимо только удаление !r при замене raw payload на числовой byte count."
    if before_value.format_spec is not None or after_value.format_spec is not None:
        return "Format specifier в точечной metadata-замене запрещён."
    if not _is_exact_len_wrapper(before_value.value, after_value.value):
        return "Разрешена только точная замена raw expression на len(raw expression)."
    return None


class _ApprovedExpressionNormalizer(_MessageNormalizer):
    def __init__(
        self,
        message_keys: set[NodeKey],
        approved_expression_keys: set[NodeKey],
    ):
        super().__init__(message_keys)
        self.approved_expression_keys = approved_expression_keys

    def visit_JoinedStr(self, node: ast.JoinedStr) -> ast.AST:
        if self._key(node, "fstring") not in self.approved_expression_keys:
            return super().visit_JoinedStr(node)

        values: list[ast.AST] = [ast.Constant(value="<LITERAL>")]
        for value in node.values:
            if isinstance(value, ast.FormattedValue):
                values.append(
                    ast.FormattedValue(
                        value=ast.Constant(value="<APPROVED_METADATA_EXPRESSION>"),
                        conversion=-1,
                        format_spec=None,
                    )
                )
                values.append(ast.Constant(value="<LITERAL>"))
        return ast.copy_location(ast.JoinedStr(values=values), node)


def _normalized_ast(
    source: str,
    message_keys: set[NodeKey],
    approved_expression_keys: set[NodeKey],
) -> str:
    tree = ast.parse(source)
    tree = _ApprovedExpressionNormalizer(
        message_keys,
        approved_expression_keys,
    ).visit(tree)
    ast.fix_missing_locations(tree)
    return ast.dump(tree, include_attributes=False)


def _update_report(outputs: dict[str, bytes], metrics: dict[str, Any]) -> None:
    status = "FAIL" if any(metrics.get(key) for key in BLOCKING_METRICS) else "PASS"
    metric_name = "stage8a_control_flow_mismatches"
    report_lines = outputs["report.md"].decode("utf-8").splitlines()
    for index, line in enumerate(report_lines):
        if line.startswith("Статус: **"):
            report_lines[index] = f"Статус: **{status}**"
        elif line.startswith(f"- {metric_name}:"):
            report_lines[index] = f"- {metric_name}: {metrics[metric_name]}"
    outputs["report.md"] = ("\n".join(report_lines) + "\n").encode("utf-8")


def apply_stage8a_control_flow_policy(
    outputs: dict[str, bytes],
    metrics: dict[str, Any],
    *,
    root: Path,
    base_sha: str,
) -> tuple[dict[str, bytes], dict[str, Any], list[str]]:
    outputs = dict(outputs)
    metrics = dict(metrics)
    findings: list[dict[str, Any]] = json.loads(outputs["semantic-findings.json"])
    mismatch_paths = {
        finding["path"]
        for finding in findings
        if finding.get("kind") == "control_flow_mismatch"
    }

    approved_paths: set[str] = set()
    approved_entries: list[dict[str, Any]] = []
    errors: list[str] = []

    for path, identifiers in APPROVED_METADATA_EXPRESSION_POLICY.items():
        if path not in mismatch_paths:
            continue

        head_source = (root / path).read_text(encoding="utf-8")
        base_source = _read_git_file(root, base_sha, path)
        head_scanner = RuntimeScanner(path, head_source)
        base_scanner = RuntimeScanner(path, base_source)
        head_rows = {row["stable_identifier"]: row for row in head_scanner.scan()}
        base_rows = {row["stable_identifier"]: row for row in base_scanner.scan()}
        base_approved_keys: set[NodeKey] = set()
        head_approved_keys: set[NodeKey] = set()
        path_entries: list[dict[str, Any]] = []
        path_errors: list[str] = []

        for identifier in identifiers:
            if identifier not in base_rows or identifier not in head_rows:
                path_errors.append(f"{identifier}: policy-точка отсутствует в base или head.")
                continue
            base_key = base_scanner.node_keys.get(identifier)
            head_key = head_scanner.node_keys.get(identifier)
            if base_key is None or head_key is None:
                path_errors.append(f"{identifier}: policy-точка не является статическим f-string.")
                continue
            before = _find_fstring(base_scanner.tree, base_key)
            after = _find_fstring(head_scanner.tree, head_key)
            if before is None or after is None:
                path_errors.append(f"{identifier}: не удалось разрешить f-string по AST key.")
                continue
            error = _validate_metadata_expression_change(before, after)
            if error is not None:
                path_errors.append(f"{identifier}: {error}")
                continue
            base_approved_keys.add(base_key)
            head_approved_keys.add(head_key)
            path_entries.append(
                {
                    "kind": "approved_metadata_expression_change",
                    "path": path,
                    "stable_identifier": identifier,
                    "before": base_rows[identifier]["message_or_template"],
                    "after": head_rows[identifier]["message_or_template"],
                    "evidence": "Разрешена только замена raw payload на byte-count metadata через len(raw).",
                }
            )

        if path_errors:
            errors.extend(f"{path}: {error}" for error in path_errors)
            continue

        base_message_ids = {
            row["stable_identifier"]
            for row in base_rows.values()
            if row["classification"] == "stage8a_first_party_message"
        }
        base_message_keys = {
            base_scanner.node_keys[identifier]
            for identifier in base_message_ids
            if identifier in base_scanner.node_keys
        }
        head_message_keys = {
            head_scanner.node_keys[identifier]
            for identifier in base_message_ids
            if identifier in head_scanner.node_keys
        }
        if _normalized_ast(
            base_source,
            base_message_keys,
            base_approved_keys,
        ) != _normalized_ast(
            head_source,
            head_message_keys,
            head_approved_keys,
        ):
            errors.append(
                f"{path}: после точечного metadata-нормализатора остался AST-delta."
            )
            continue

        approved_paths.add(path)
        approved_entries.extend(path_entries)

    unapproved_findings = [
        finding
        for finding in findings
        if finding.get("kind") != "control_flow_mismatch"
        or finding.get("path") not in approved_paths
    ]
    policy_error_findings = [
        {"kind": "control_flow_policy_error", "evidence": error}
        for error in errors
    ]
    outputs["semantic-findings.json"] = _json_bytes(
        [*unapproved_findings, *approved_entries, *policy_error_findings]
    )

    remaining_mismatch_paths = {
        finding["path"]
        for finding in unapproved_findings
        if finding.get("kind") == "control_flow_mismatch"
    }
    metrics["stage8a_control_flow_mismatches"] = (
        len(remaining_mismatch_paths) + len(errors)
    )
    outputs["metrics.json"] = _json_bytes(metrics)

    contract = json.loads(outputs["contract.json"])
    contract["translation_only"] = metrics["stage8a_control_flow_mismatches"] == 0
    contract["approved_metadata_expression_changes"] = len(approved_entries)
    contract["control_flow_policy_preserved"] = not errors
    outputs["contract.json"] = _json_bytes(contract)
    outputs["control-flow-policy.json"] = _json_bytes(
        {
            "status": "PASS" if not errors and not remaining_mismatch_paths else "FAIL",
            "approved_paths": sorted(approved_paths),
            "approved_points": approved_entries,
            "remaining_mismatch_paths": sorted(remaining_mismatch_paths),
            "errors": errors,
        }
    )
    _update_report(outputs, metrics)
    return outputs, metrics, errors
