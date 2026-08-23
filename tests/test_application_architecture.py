from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

from tests.import_inspection import absolute_import_candidates, imports_for_path

ROOT = Path(__file__).resolve().parents[1]
APPLICATION_ROOT = ROOT / "module" / "application"
FORBIDDEN_IMPORT_ROOTS = {"fastapi", "mcp", "pywebio", "starlette"}


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


def test_existing_webui_and_mcp_production_wiring_remains_independent():
    app_path = ROOT / "module" / "webui" / "app.py"
    mcp_path = ROOT / "mcp_server_sse.py"
    assert app_path.is_file(), app_path
    assert mcp_path.is_file(), mcp_path
    app_tree = ast.parse(app_path.read_text(encoding="utf-8"))
    mcp_source = mcp_path.read_text(encoding="utf-8")
    mcp_tree = ast.parse(mcp_source)

    application_imports: set[str] = set()
    for path, tree in ((app_path, app_tree), (mcp_path, mcp_tree)):
        for node in ast.walk(tree):
            candidates = absolute_import_candidates(ROOT, path, node)
            application_imports.update(
                name
                for name in candidates
                if name == "module.application"
                or name.startswith("module.application.")
            )
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
        (
            node
            for node in mcp_tree.body
            if (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == "TOOL_HANDLERS"
                    for target in node.targets
                )
            )
            or (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "TOOL_HANDLERS"
            )
        ),
        None,
    )
    assert handler_assignment is not None, mcp_path
    assert isinstance(handler_assignment.value, ast.Dict)
    handler_names = {
        key.value
        for key in handler_assignment.value.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }

    assert not application_imports, application_imports
    assert "/mcp" in mounted_paths
    assert {
        "list_instances",
        "get_status",
        "list_tasks",
        "get_task_help",
    } <= handler_names


def test_application_import_candidates_cover_from_module_form():
    node = ast.parse("from module import application as app").body[0]
    assert "module.application" in absolute_import_candidates(
        ROOT, ROOT / "probe.py", node
    )
