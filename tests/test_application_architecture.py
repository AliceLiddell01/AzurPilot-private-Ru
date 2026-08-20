from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APPLICATION_ROOT = ROOT / "module" / "application"
FORBIDDEN_IMPORT_ROOTS = {"fastapi", "mcp", "pywebio", "starlette"}


def _import_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def test_application_layer_has_no_transport_framework_imports():
    for path in APPLICATION_ROOT.glob("*.py"):
        assert _import_roots(path).isdisjoint(FORBIDDEN_IMPORT_ROOTS), path


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
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr


def test_existing_webui_and_mcp_production_wiring_remains_independent():
    app_tree = ast.parse(
        (ROOT / "module" / "webui" / "app.py").read_text(encoding="utf-8")
    )
    mcp_path = ROOT / "mcp_server_sse.py"
    mcp_source = mcp_path.read_text(encoding="utf-8")
    mcp_tree = ast.parse(mcp_source)

    application_imports = {
        node.module
        for tree in (app_tree, mcp_tree)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.startswith("module.application")
    }
    mounted_paths = {
        node.args[0].value
        for node in ast.walk(app_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "mount"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }
    handler_assignment = next(
        node
        for node in mcp_tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "TOOL_HANDLERS"
            for target in node.targets
        )
    )
    assert isinstance(handler_assignment.value, ast.Dict)
    handler_names = {
        key.value
        for key in handler_assignment.value.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }

    assert not application_imports
    assert "/mcp" in mounted_paths
    assert {
        "list_instances",
        "get_status",
        "list_tasks",
        "get_task_help",
    } <= handler_names
