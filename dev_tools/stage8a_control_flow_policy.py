from __future__ import annotations

import ast
import json
import re
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
LocationKey = tuple[int, int, int, int]
CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
WEBUI_LIVE_SECURITY_PATH = "module/webui/api.py"
WEBUI_LIVE_SECURITY_HELPER_NAMES = (
    "_websocket_client_host",
    "_is_local_live_websocket",
    "_reject_nonlocal_live_websocket",
    "_ws_live_screenshot_guarded",
    "_ws_live_control_guarded",
)
WEBUI_LIVE_SECURITY_ROUTE_BINDINGS = {
    "/ws/live_screenshot": ("_ws_live_screenshot_guarded", "ws_live_screenshot"),
    "/ws/live_control": ("_ws_live_control_guarded", "ws_live_control"),
}
WEBUI_LIVE_SECURITY_HELPER_SOURCE = r"""\
def _websocket_client_host(websocket) -> str:
    client = getattr(websocket, "client", None)
    host = getattr(client, "host", "") if client is not None else ""
    return str(host or "").strip()


def _is_local_live_websocket(websocket) -> bool:
    host = _websocket_client_host(websocket)
    if not host:
        return False
    normalized = host.strip("[]").split("%", 1)[0]
    if normalized.lower() == "localhost":
        return True
    try:
        packed = socket.inet_pton(socket.AF_INET, normalized)
    except OSError:
        packed = None
    if packed is not None:
        return packed[0] == 127
    try:
        packed = socket.inet_pton(socket.AF_INET6, normalized)
    except OSError:
        return False
    return packed == (b"\x00" * 15 + b"\x01") or (
        packed[:12] == (b"\x00" * 10 + b"\xff\xff")
        and packed[12] == 127
    )


async def _reject_nonlocal_live_websocket(websocket) -> bool:
    if _is_local_live_websocket(websocket):
        return False
    await websocket.accept()
    await websocket.send_text(json.dumps({
        "type": "error",
        "message": (
            "Предпросмотр и управление устройством доступны только из локальной WebUI. "
            "Удалённый доступ требует отдельного аутентифицированного transport-контракта."
        ),
    }, ensure_ascii=False))
    await websocket.close(code=4403)
    return True


async def _ws_live_screenshot_guarded(websocket):
    if await _reject_nonlocal_live_websocket(websocket):
        return
    await ws_live_screenshot(websocket)


async def _ws_live_control_guarded(websocket):
    if await _reject_nonlocal_live_websocket(websocket):
        return
    await ws_live_control(websocket)
"""


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


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


def _node_key(node: ast.AST, kind: str) -> NodeKey:
    return (
        node.lineno,
        node.col_offset,
        node.end_lineno,
        node.end_col_offset,
        kind,
    )


def _location_key(node: ast.AST) -> LocationKey:
    return (node.lineno, node.col_offset, node.end_lineno, node.end_col_offset)


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


def _exception_context_payload(node: ast.AST) -> ast.AST | None:
    if (
        not isinstance(node, ast.Call)
        or not isinstance(node.func, ast.Name)
        or node.func.id != "str"
        or len(node.args) != 1
        or node.keywords
        or not isinstance(node.args[0], ast.JoinedStr)
    ):
        return None
    formatted = [
        value
        for value in node.args[0].values
        if isinstance(value, ast.FormattedValue)
    ]
    static_text = "".join(
        value.value
        for value in node.args[0].values
        if isinstance(value, ast.Constant) and isinstance(value.value, str)
    )
    if len(formatted) != 1 or not CYRILLIC_RE.search(static_text):
        return None
    item = formatted[0]
    if item.conversion != -1 or item.format_spec is not None:
        return None
    if not isinstance(item.value, ast.Name):
        return None
    return item.value


def _find_exception_context_wrapper(
    tree: ast.AST,
    *,
    line: int,
    call_kind: str,
) -> ast.Call | None:
    matches: list[ast.Call] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and node.lineno == line
            and _call_name(node.func) == call_kind
            and node.args
            and _exception_context_payload(node.args[0]) is not None
        ):
            matches.append(node.args[0])
    return matches[0] if len(matches) == 1 else None


class _ApprovedExpressionNormalizer(_MessageNormalizer):
    def __init__(
        self,
        message_keys: set[NodeKey],
        approved_expression_keys: set[NodeKey],
        exception_context_keys: set[LocationKey],
    ):
        super().__init__(message_keys)
        self.approved_expression_keys = approved_expression_keys
        self.exception_context_keys = exception_context_keys

    def visit_Call(self, node: ast.Call) -> ast.AST:
        if _location_key(node) in self.exception_context_keys:
            payload = _exception_context_payload(node)
            if payload is not None:
                return self.visit(payload)
        return super().visit_Call(node)

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


def _top_level_functions(tree: ast.Module) -> dict[str, ast.AST]:
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _webui_live_route_bindings(tree: ast.AST) -> dict[str, list[str]]:
    bindings: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.Call)
            or _call_name(node.func) != "WebSocketRoute"
            or len(node.args) < 2
            or not isinstance(node.args[0], ast.Constant)
            or not isinstance(node.args[0].value, str)
            or not isinstance(node.args[1], ast.Name)
        ):
            continue
        route = node.args[0].value
        if route in WEBUI_LIVE_SECURITY_ROUTE_BINDINGS:
            bindings.setdefault(route, []).append(node.args[1].id)
    return bindings


def _validate_webui_live_security_guard(
    base_source: str,
    head_source: str,
) -> list[str]:
    base_tree = ast.parse(base_source)
    head_tree = ast.parse(head_source)
    expected_tree = ast.parse(WEBUI_LIVE_SECURITY_HELPER_SOURCE)
    base_functions = _top_level_functions(base_tree)
    head_functions = _top_level_functions(head_tree)
    expected_functions = _top_level_functions(expected_tree)
    errors: list[str] = []

    for name in WEBUI_LIVE_SECURITY_HELPER_NAMES:
        if name in base_functions:
            errors.append(f"{name}: helper unexpectedly exists in immutable base.")
        actual = head_functions.get(name)
        expected = expected_functions.get(name)
        if actual is None or expected is None:
            errors.append(f"{name}: required security helper is missing.")
            continue
        if ast.dump(actual, include_attributes=False) != ast.dump(
            expected, include_attributes=False
        ):
            errors.append(f"{name}: helper AST differs from reviewed security contract.")

    base_bindings = _webui_live_route_bindings(base_tree)
    head_bindings = _webui_live_route_bindings(head_tree)
    for route, (guarded, original) in WEBUI_LIVE_SECURITY_ROUTE_BINDINGS.items():
        if base_bindings.get(route) != [original]:
            errors.append(f"{route}: immutable base route is not bound exactly to {original}.")
        if head_bindings.get(route) != [guarded]:
            errors.append(f"{route}: head route is not bound exactly to {guarded}.")
    return errors


class _WebuiLiveSecurityNormalizer(ast.NodeTransformer):
    def visit_Module(self, node: ast.Module) -> ast.AST:
        body: list[ast.stmt] = []
        for item in node.body:
            if (
                isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and item.name in WEBUI_LIVE_SECURITY_HELPER_NAMES
            ):
                continue
            visited = self.visit(item)
            if isinstance(visited, ast.stmt):
                body.append(visited)
        node.body = body
        return node

    def visit_Call(self, node: ast.Call) -> ast.AST:
        node = self.generic_visit(node)
        if (
            _call_name(node.func) == "WebSocketRoute"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and isinstance(node.args[1], ast.Name)
        ):
            route = node.args[0].value
            binding = WEBUI_LIVE_SECURITY_ROUTE_BINDINGS.get(route)
            if binding is not None and node.args[1].id == binding[0]:
                node.args[1] = ast.copy_location(
                    ast.Name(id=binding[1], ctx=ast.Load()),
                    node.args[1],
                )
        return node


def _normalized_ast(
    source: str,
    message_keys: set[NodeKey],
    approved_expression_keys: set[NodeKey],
    exception_context_keys: set[LocationKey] | None = None,
    *,
    normalize_webui_security: bool = False,
) -> str:
    tree = ast.parse(source)
    if normalize_webui_security:
        tree = _WebuiLiveSecurityNormalizer().visit(tree)
    tree = _ApprovedExpressionNormalizer(
        message_keys,
        approved_expression_keys,
        exception_context_keys or set(),
    ).visit(tree)
    ast.fix_missing_locations(tree)
    return ast.dump(tree, include_attributes=False)


def _update_report(outputs: dict[str, bytes], metrics: dict[str, Any]) -> None:
    status = "FAIL" if any(metrics.get(key) for key in BLOCKING_METRICS) else "PASS"
    report_lines = outputs["report.md"].decode("utf-8").splitlines()
    updated = {
        "stage8a_control_flow_mismatches": False,
        "stage8a_raw_payload_violations": False,
    }
    for index, line in enumerate(report_lines):
        if line.startswith("Статус: **"):
            report_lines[index] = f"Статус: **{status}**"
        for metric_name in updated:
            if line.startswith(f"- {metric_name}:"):
                report_lines[index] = f"- {metric_name}: {metrics[metric_name]}"
                updated[metric_name] = True
    for metric_name, found in updated.items():
        if not found:
            report_lines.extend(("", f"- {metric_name}: {metrics[metric_name]}"))
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
    raw_change_ids = {
        finding["stable_identifier"]
        for finding in findings
        if finding.get("kind") == "raw_expression_changed"
    }

    candidate_paths = set(mismatch_paths)
    candidate_paths.update(APPROVED_METADATA_EXPRESSION_POLICY)
    approved_paths: set[str] = set()
    approved_raw_ids: set[str] = set()
    approved_entries: list[dict[str, Any]] = []
    errors: list[str] = []

    for path in sorted(candidate_paths):
        if not (root / path).is_file():
            continue
        head_source = (root / path).read_text(encoding="utf-8")
        base_source = _read_git_file(root, base_sha, path)
        head_scanner = RuntimeScanner(path, head_source)
        base_scanner = RuntimeScanner(path, base_source)
        head_rows = {row["stable_identifier"]: row for row in head_scanner.scan()}
        base_rows = {row["stable_identifier"]: row for row in base_scanner.scan()}
        base_metadata_keys: set[NodeKey] = set()
        head_metadata_keys: set[NodeKey] = set()
        head_exception_keys: set[LocationKey] = set()
        path_entries: list[dict[str, Any]] = []
        path_errors: list[str] = []
        security_guard_approved = False

        if path == WEBUI_LIVE_SECURITY_PATH and path in mismatch_paths:
            security_errors = _validate_webui_live_security_guard(
                base_source,
                head_source,
            )
            if security_errors:
                path_errors.extend(security_errors)
            else:
                security_guard_approved = True
                path_entries.append(
                    {
                        "kind": "approved_webui_live_security_guard",
                        "path": path,
                        "evidence": (
                            "Exact loopback-only wrapper for live screenshot/control routes; "
                            "original handlers and their logger sequence remain unchanged."
                        ),
                    }
                )

        for identifier in APPROVED_METADATA_EXPRESSION_POLICY.get(path, ()):
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
            base_metadata_keys.add(base_key)
            head_metadata_keys.add(head_key)
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

        for identifier in sorted(set(base_rows) & set(head_rows)):
            if identifier not in raw_change_ids:
                continue
            before = base_rows[identifier]
            after = head_rows[identifier]
            wrapper = _find_exception_context_wrapper(
                head_scanner.tree,
                line=int(after["line"]),
                call_kind=str(after["call_kind"]),
            )
            if wrapper is None:
                continue
            payload = _exception_context_payload(wrapper)
            if payload is None:
                path_errors.append(f"{identifier}: invalid exception-context wrapper.")
                continue
            if ast.dump(payload, include_attributes=False) != before.get("raw_expression"):
                path_errors.append(
                    f"{identifier}: exception-context wrapper changed the raw exception expression."
                )
                continue
            if ast.dump(wrapper, include_attributes=False) != after.get("raw_expression"):
                path_errors.append(
                    f"{identifier}: wrapper AST does not match the audited head expression."
                )
                continue
            head_exception_keys.add(_location_key(wrapper))
            approved_raw_ids.add(identifier)
            path_entries.append(
                {
                    "kind": "approved_exception_context_wrapper",
                    "path": path,
                    "stable_identifier": identifier,
                    "call_kind": after["call_kind"],
                    "evidence": (
                        "Exact str(f'Russian first-party context: {exception}') wrapper; "
                        "raw exception expression, severity and call position are preserved."
                    ),
                }
            )

        if path_errors:
            errors.extend(f"{path}: {error}" for error in path_errors)
            continue
        if not path_entries:
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
            base_metadata_keys,
            set(),
        ) != _normalized_ast(
            head_source,
            head_message_keys,
            head_metadata_keys,
            head_exception_keys,
            normalize_webui_security=security_guard_approved,
        ):
            errors.append(
                f"{path}: после точечных metadata/exception нормализаторов остался AST-delta."
            )
            continue

        approved_paths.add(path)
        approved_entries.extend(path_entries)

    unapproved_findings = [
        finding
        for finding in findings
        if not (
            finding.get("kind") == "control_flow_mismatch"
            and finding.get("path") in approved_paths
        )
        and not (
            finding.get("kind") == "raw_expression_changed"
            and finding.get("stable_identifier") in approved_raw_ids
        )
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
    remaining_raw_changes = [
        finding
        for finding in unapproved_findings
        if finding.get("kind") == "raw_expression_changed"
    ]
    metrics["stage8a_control_flow_mismatches"] = (
        len(remaining_mismatch_paths) + len(errors)
    )
    metrics["stage8a_raw_payload_violations"] = len(remaining_raw_changes)
    outputs["metrics.json"] = _json_bytes(metrics)

    contract = json.loads(outputs["contract.json"])
    approved_security_changes = sum(
        entry["kind"] == "approved_webui_live_security_guard"
        for entry in approved_entries
    )
    contract["translation_only"] = (
        metrics["stage8a_control_flow_mismatches"] == 0
        and approved_security_changes == 0
    )
    contract["approved_security_control_flow_changes"] = approved_security_changes
    contract["raw_expression_contract_preserved"] = (
        metrics["stage8a_raw_payload_violations"] == 0
    )
    contract["approved_metadata_expression_changes"] = sum(
        entry["kind"] == "approved_metadata_expression_change"
        for entry in approved_entries
    )
    contract["approved_exception_context_wrappers"] = sum(
        entry["kind"] == "approved_exception_context_wrapper"
        for entry in approved_entries
    )
    contract["control_flow_policy_preserved"] = not errors
    outputs["contract.json"] = _json_bytes(contract)
    outputs["control-flow-policy.json"] = _json_bytes(
        {
            "status": (
                "PASS"
                if not errors and not remaining_mismatch_paths and not remaining_raw_changes
                else "FAIL"
            ),
            "approved_paths": sorted(approved_paths),
            "approved_points": approved_entries,
            "remaining_mismatch_paths": sorted(remaining_mismatch_paths),
            "remaining_raw_expression_changes": remaining_raw_changes,
            "errors": errors,
        }
    )
    _update_report(outputs, metrics)
    return outputs, metrics, errors
