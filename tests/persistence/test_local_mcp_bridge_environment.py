from __future__ import annotations

from pathlib import Path

import pytest

from module.application.errors import StorageConfigurationError
from module.mcp_shared.local_http_auth import (
    LocalHttpAuthError,
    read_local_mcp_bridge_caller_token,
)
from module.mcp_shared.windows_mcp_bridge_contract import BRIDGE_CALLER_TOKEN_ENV_VAR
from module.persistence import local_environment
from module.persistence.local_environment import (
    read_local_environment_subset,
    write_local_mcp_bridge_caller_token,
)

_DEV_TOKEN = "dev-internal-token-012345678901234567890123456789"
_GAME_TOKEN = "game-internal-token-01234567890123456789012345678"
_CALLER_TOKEN = "bridge-caller-token-01234567890123456789012345678"


def _project(tmp_path: Path) -> Path:
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text("", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    (tmp_path / ".env").write_text(
        f"AZURPILOT_DEV_LOCAL_MCP_TOKEN={_DEV_TOKEN}\n"
        f"AZURPILOT_GAME_LOCAL_MCP_TOKEN={_GAME_TOKEN}\n",
        encoding="utf-8",
    )
    return tmp_path


def _allow_test_permissions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        local_environment, "_require_secure_permissions", lambda *_: None
    )
    monkeypatch.setattr(
        local_environment, "_restrict_windows_environment_file", lambda *_: None
    )


def test_writer_adds_caller_token_without_replacing_internal_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _allow_test_permissions(monkeypatch)
    root = _project(tmp_path)

    write_local_mcp_bridge_caller_token(root, _CALLER_TOKEN)

    values = read_local_environment_subset(
        root / ".env",
        keys=(
            BRIDGE_CALLER_TOKEN_ENV_VAR,
            "AZURPILOT_DEV_LOCAL_MCP_TOKEN",
            "AZURPILOT_GAME_LOCAL_MCP_TOKEN",
        ),
    )
    assert values == {
        BRIDGE_CALLER_TOKEN_ENV_VAR: _CALLER_TOKEN,
        "AZURPILOT_DEV_LOCAL_MCP_TOKEN": _DEV_TOKEN,
        "AZURPILOT_GAME_LOCAL_MCP_TOKEN": _GAME_TOKEN,
    }
    assert read_local_mcp_bridge_caller_token(root) == _CALLER_TOKEN


def test_writer_rejects_internal_token_as_bridge_caller(tmp_path: Path) -> None:
    root = _project(tmp_path)
    before = (root / ".env").read_bytes()

    with pytest.raises(StorageConfigurationError):
        write_local_mcp_bridge_caller_token(root, _DEV_TOKEN)

    assert (root / ".env").read_bytes() == before


def test_reader_rejects_caller_token_equal_to_internal_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _allow_test_permissions(monkeypatch)
    root = _project(tmp_path)
    (root / ".env").write_text(
        f"AZURPILOT_DEV_LOCAL_MCP_TOKEN={_DEV_TOKEN}\n"
        f"AZURPILOT_GAME_LOCAL_MCP_TOKEN={_GAME_TOKEN}\n"
        f"{BRIDGE_CALLER_TOKEN_ENV_VAR}={_DEV_TOKEN}\n",
        encoding="utf-8",
    )

    with pytest.raises(LocalHttpAuthError):
        read_local_mcp_bridge_caller_token(root)
