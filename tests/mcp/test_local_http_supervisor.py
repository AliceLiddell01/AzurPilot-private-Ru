from __future__ import annotations

import sys
from pathlib import Path

import pytest

from module.mcp_shared.local_http_supervisor import (
    LocalHttpService,
    LocalHttpSupervisor,
    LocalHttpSupervisorError,
    _release_lock,
    _try_lock,
)


def _supervisor(tmp_path: Path) -> LocalHttpSupervisor:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    return LocalHttpSupervisor(tmp_path, python_executable=sys.executable)


def test_supervisor_lock_allows_only_one_owner(tmp_path: Path) -> None:
    supervisor = _supervisor(tmp_path)
    first = _try_lock(supervisor.lock_path)
    assert first is not None
    try:
        assert _try_lock(supervisor.lock_path) is None
    finally:
        _release_lock(first)
    second = _try_lock(supervisor.lock_path)
    assert second is not None
    _release_lock(second)


def test_supervisor_validates_both_bearer_environment_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = _supervisor(tmp_path)
    monkeypatch.setenv("AZURPILOT_DEV_LOCAL_MCP_TOKEN", "dev-test-token")
    monkeypatch.delenv("AZURPILOT_GAME_LOCAL_MCP_TOKEN", raising=False)

    with pytest.raises(LocalHttpSupervisorError, match="AZURPILOT_GAME_LOCAL_MCP_TOKEN"):
        supervisor._validate()


def test_supervisor_commands_are_direct_project_python_http_entrypoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = _supervisor(tmp_path)
    monkeypatch.setenv("AZURPILOT_DEV_LOCAL_MCP_TOKEN", "dev-test-token")
    monkeypatch.setenv("AZURPILOT_GAME_LOCAL_MCP_TOKEN", "game-test-token")
    supervisor._validate()

    assert supervisor._command(
        LocalHttpService(
            "azurpilot-game",
            "module.game_mcp.local_http",
            8776,
            "AZURPILOT_GAME_LOCAL_MCP_TOKEN",
        )
    ) == [sys.executable, "-u", "-m", "module.game_mcp.local_http"]
