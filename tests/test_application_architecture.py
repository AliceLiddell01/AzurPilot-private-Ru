from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

from tests.import_inspection import absolute_import_candidates, imports_for_path

ROOT = Path(__file__).resolve().parents[1]
APPLICATION_ROOT = ROOT / "module" / "application"
FORBIDDEN_IMPORT_ROOTS = {"fastapi", "mcp", "pywebio", "starlette"}
ROUTE_REGISTRATION_METHODS = {"mount", "add_route", "add_websocket_route"}


def _constant_string_value(
    node: ast.AST, bindings: dict[str, str | frozenset[str]]
) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        value = bindings.get(node.id)
        return value if isinstance(value, str) else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _constant_string_value(node.left, bindings)
        right = _constant_string_value(node.right, bindings)
        if left is not None and right is not None:
            return left + right
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
                continue
            if isinstance(value, ast.FormattedValue):
                formatted = _constant_string_value(value.value, bindings)
                if formatted is None:
                    return None
                parts.append(formatted)
                continue
            return None
        return "".join(parts)
    return None


def _constant_mapping_keys(
    node: ast.AST, bindings: dict[str, str | frozenset[str]]
) -> frozenset[str] | None:
    if isinstance(node, ast.Name):
        value = bindings.get(node.id)
        return value if isinstance(value, frozenset) else None
    if isinstance(node, ast.Constant) and node.value is None:
        return frozenset()
    if not isinstance(node, ast.Dict):
        return None
    keys: set[str] = set()
    for key in node.keys:
        if key is None:
            return None
        value = _constant_string_value(key, bindings)
        if value is None:
            return None
        keys.add(value)
    return frozenset(keys)


class _ScopeCollector(ast.NodeVisitor):
    def __init__(self, inherited: dict[str, str | frozenset[str]]):
        self.bindings = dict(inherited)
        self.calls: list[tuple[ast.Call, dict[str, str | frozenset[str]]]] = []
        self.nested_scopes: list[ast.AST] = []

    def _bind_targets(
        self, targets: list[ast.expr], value_node: ast.AST
    ) -> None:
        value = _constant_string_value(value_node, self.bindings)
        if value is None:
            value = _constant_mapping_keys(value_node, self.bindings)
        if value is None:
            return
        for target in targets:
            if isinstance(target, ast.Name):
                self.bindings[target.id] = value

    def visit_Assign(self, node: ast.Assign) -> None:
        self._bind_targets(node.targets, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self._bind_targets([node.target], node.value)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        self.calls.append((node, dict(self.bindings)))
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.nested_scopes.append(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.nested_scopes.append(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.nested_scopes.append(node)


def _scope_body(scope: ast.AST) -> list[ast.stmt]:
    return getattr(scope, "body", [])


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def _registered_route_paths(tree: ast.AST) -> tuple[set[str], list[str]]:
    paths: set[str] = set()
    unresolved: list[str] = []

    def collect_scope(scope: ast.AST, inherited: dict[str, str | frozenset[str]]) -> None:
        collector = _ScopeCollector(inherited)
        for statement in _scope_body(scope):
            collector.visit(statement)
        for call, bindings in collector.calls:
            call_name = _call_name(call)
            if call_name in ROUTE_REGISTRATION_METHODS:
                if not call.args:
                    unresolved.append(ast.unparse(call))
                    continue
                path = _constant_string_value(call.args[0], bindings)
                if path is None:
                    unresolved.append(ast.unparse(call.args[0]))
                else:
                    paths.add(path)
            if call_name != "asgi_app":
                continue
            static_mounts = next(
                (
                    keyword.value
                    for keyword in call.keywords
                    if keyword.arg == "static_mounts"
                ),
                None,
            )
            if static_mounts is None:
                continue
            mount_paths = _constant_mapping_keys(static_mounts, bindings)
            if mount_paths is None:
                unresolved.append(ast.unparse(static_mounts))
            else:
                paths.update(mount_paths)
        for nested_scope in collector.nested_scopes:
            collect_scope(nested_scope, collector.bindings)

    collect_scope(tree, {})
    return paths, unresolved


def _is_mcp_route(path: str) -> bool:
    normalized = path.rstrip("/") or "/"
    return normalized == "/mcp" or normalized.startswith("/mcp/")


def _import_roots(path: Path) -> set[str]:
    return {name.split(".", 1)[0] for name in imports_for_path(ROOT, path)}


def test_application_layer_has_no_transport_framework_imports():
    paths = tuple(APPLICATION_ROOT.rglob("*.py"))
    assert paths, APPLICATION_ROOT
    for path in paths:
        assert _import_roots(path).isdisjoint(FORBIDDEN_IMPORT_ROOTS), path


def test_application_layer_has_no_persistence_adapter_imports():
    paths = tuple(APPLICATION_ROOT.rglob("*.py"))
    assert paths, APPLICATION_ROOT
    for path in paths:
        candidates = imports_for_path(ROOT, path)
        assert not any(
            name == "module.persistence" or name.startswith("module.persistence.")
            for name in candidates
        ), path


def test_relative_imports_are_resolved_before_architecture_checks():
    application_node = ast.parse("from ..persistence import runtime").body[0]
    statistics_node = ast.parse("from . import cl1_database").body[0]

    assert "module.persistence" in absolute_import_candidates(
        ROOT, APPLICATION_ROOT / "probe.py", application_node
    )
    assert "module.statistics.cl1_database" in absolute_import_candidates(
        ROOT, ROOT / "module" / "statistics" / "probe.py", statistics_node
    )


def test_importing_application_package_does_not_load_legacy_runtime(tmp_path: Path):
    script = f"""
import sys
sys.path.insert(0, {str(ROOT)!r})
import module.application
for name in (
    'module.config.utils',
    'module.webui.process_manager',
    'module.device',
):
    assert name not in sys.modules, name
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr


def test_legacy_mcp_entrypoint_is_absent_and_webui_has_no_mcp_mount():
    app_path = ROOT / "module" / "webui" / "app.py"
    assert app_path.is_file(), app_path
    assert not (ROOT / "mcp_server_sse.py").exists()
    app_tree = ast.parse(app_path.read_text(encoding="utf-8"))
    webui_application_imports: set[str] = set()
    for node in ast.walk(app_tree):
        candidates = absolute_import_candidates(ROOT, app_path, node)
        webui_application_imports.update(
            name
            for name in candidates
            if name == "module.application"
            or name.startswith("module.application.")
        )
    registered_route_paths, unresolved_route_paths = _registered_route_paths(app_tree)
    assert not webui_application_imports, webui_application_imports
    assert not unresolved_route_paths, unresolved_route_paths
    assert _is_mcp_route("/mcp")
    assert _is_mcp_route("/mcp/")
    assert _is_mcp_route("/mcp/messages")
    assert not _is_mcp_route("/mcpx")
    assert not {
        path for path in registered_route_paths if _is_mcp_route(path)
    }, sorted(registered_route_paths)
    assert (ROOT / "module" / "game_mcp" / "__init__.py").is_file()
    assert (ROOT / "module" / "dev_mcp" / "__init__.py").is_file()
    assert (ROOT / "module" / "mcp_shared" / "remote.py").is_file()


def test_route_analysis_uses_scope_specific_bindings():
    tree = ast.parse(
        """
MCP_PATH = "/mcp"

def register(application):
    MCP_PATH = "/static"
    application.mount(MCP_PATH, object())
    MCP_PATH = "/mcp/late"
"""
    )

    paths, unresolved = _registered_route_paths(tree)

    assert not unresolved, unresolved
    assert paths == {"/static"}


def test_application_import_candidates_cover_from_module_form():
    node = ast.parse("from module import application as app").body[0]
    assert "module.application" in absolute_import_candidates(
        ROOT, ROOT / "probe.py", node
    )
