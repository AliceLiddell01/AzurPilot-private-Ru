from __future__ import annotations

import ast
from pathlib import Path

from module.game_mcp.server import tool_definitions

_ROOT = Path(__file__).resolve().parents[1]
_GAME_MCP_ROOT = _ROOT / "module" / "game_mcp"


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    # Эта проверка видит только прямые импорты файлов Game MCP, а не транзитивные зависимости.
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, (
                "Проверка архитектуры Game MCP запрещает относительные импорты"
            )
            if node.module:
                names.add(node.module)
                names.update(
                    f"{node.module}.{alias.name}"
                    for alias in node.names
                    if alias.name != "*"
                )
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


def test_game_mcp_publishes_control_but_excludes_developer_surfaces() -> None:
    published_names = {tool.name for tool in tool_definitions()}
    assert {
        "game_start_profile",
        "game_stop_profile",
        "game_trigger_task",
        "game_clear_scheduler_queue",
        "game_update_config",
        "game_restart_emulator",
        "game_restart_runtime",
        "game_login_runtime",
        "game_restart_adb",
    } <= published_names
    for forbidden in (
        "game_restart_profile",
        "game_clear_scheduler",
        "game_get_database_status",
        "game_run_database_check",
        "game_list_database_repairs",
        "game_execute_sql",
    ):
        assert forbidden not in published_names
