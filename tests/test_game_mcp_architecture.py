from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_GAME_MCP_ROOT = _ROOT / "module" / "game_mcp"


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_game_mcp_has_no_dev_or_direct_storage_dependency() -> None:
    paths = tuple(_GAME_MCP_ROOT.rglob("*.py"))
    assert paths
    imported = set().union(*(_imported_modules(path) for path in paths))
    assert not any(
        name.startswith(("module.dev_mcp", "module.dev_runtime")) for name in imported
    )
    assert not any(
        name == "sqlalchemy" or name.startswith("sqlalchemy.") for name in imported
    )
    assert not any(
        name == "psycopg" or name.startswith(("psycopg.", "psycopg2"))
        for name in imported
    )
    assert "mcp.server.sse" not in imported


def test_game_mcp_tool_names_exclude_control_and_developer_surfaces() -> None:
    paths = tuple(_GAME_MCP_ROOT.rglob("*.py"))
    assert paths
    source = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    for forbidden in (
        "game_start_profile",
        "game_stop_profile",
        "game_restart_profile",
        "game_trigger_task",
        "game_clear_scheduler",
        "game_update_config",
        "game_get_database_status",
        "game_run_database_check",
        "game_list_database_repairs",
        "game_execute_sql",
        "GameControlService",
        "DevGameBridge",
        "DevSession",
        "Smoke",
    ):
        assert forbidden not in source
