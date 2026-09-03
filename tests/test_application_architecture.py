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


def _constant_bindings(tree: ast.AST) -> dict[str, str | frozenset[str]]:
    bindings: dict[str, str | frozenset[str]] = {}
    assignments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
    ]
    for _ in range(len(assignments) + 1):
        changed = False
        for node in assignments:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                value: str | frozenset[str] | None
                if isinstance(node, ast.Assign):
                    value = _constant_string_value(node.value, bindings)
                    if value is None:
                        value = _constant_mapping_keys(node.value, bindings)
                else:
                    value = _constant_string_value(node.value, bindings)
                    if value is None:
                        value = _constant_mapping_keys(node.value, bindings)
                if value is not None and bindings.get(target.id) != value:
                    bindings[target.id] = value
                    changed = True
        if not changed:
            break
    return bindings


def _registered_route_paths(tree: ast.AST) -> tuple[set[str], list[str]]:
    bindings = _constant_bindings(tree)
    paths: set[str] = set()
    unresolved: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr in ROUTE_REGISTRATION_METHODS:
            if not node.args:
                unresolved.append(ast.unparse(node))
                continue
            path = _constant_string_value(node.args[0], bindings)
            if path is None:
                unresolved.append(ast.unparse(node.args[0]))
            else:
                paths.add(path)
        if node.func.attr == "asgi_app":
            static_mounts = next(
                (
                    keyword.value
                    for keyword in node.keywords
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


def test_application_import_candidates_cover_from_module_form():
    node = ast.parse("from module import application as app").body[0]
    assert "module.application" in absolute_import_candidates(
        ROOT, ROOT / "probe.py", node
    )
