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
    assert not webui_application_imports, webui_application_imports
    assert "/mcp" not in mounted_paths
    assert (ROOT / "module" / "game_mcp" / "__init__.py").is_file()
    assert (ROOT / "module" / "dev_mcp" / "__init__.py").is_file()
    assert (ROOT / "module" / "mcp_shared" / "remote.py").is_file()


def test_application_import_candidates_cover_from_module_form():
    node = ast.parse("from module import application as app").body[0]
    assert "module.application" in absolute_import_candidates(
        ROOT, ROOT / "probe.py", node
    )
