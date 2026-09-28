from __future__ import annotations

import json
from pathlib import Path

import pytest

from module.application.errors import StorageConfigurationUnknownError
from module.mcp_shared import local_http_auth


def _project(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text("", encoding="utf-8")
    return tmp_path


def test_project_local_reader_never_falls_back_to_ambient_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    monkeypatch.setenv("AZURPILOT_DEV_LOCAL_MCP_TOKEN", "ambient-token")

    with pytest.raises(local_http_auth.LocalHttpAuthError):
        local_http_auth.read_local_mcp_token(root, "azurpilot-dev")


def test_project_local_reader_uses_closed_server_to_token_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    seen: list[tuple[str, ...]] = []

    def read_subset(_path: Path, *, keys: tuple[str, ...]) -> dict[str, str]:
        seen.append(keys)
        return {keys[0]: f"token-for-{keys[0]}"}

    monkeypatch.setattr(local_http_auth, "read_local_environment_subset", read_subset)

    assert local_http_auth.read_local_mcp_token(root, "azurpilot-dev") == (
        "token-for-AZURPILOT_DEV_LOCAL_MCP_TOKEN"
    )
    assert local_http_auth.read_local_mcp_token(root, "azurpilot-game") == (
        "token-for-AZURPILOT_GAME_LOCAL_MCP_TOKEN"
    )
    assert seen == [
        ("AZURPILOT_DEV_LOCAL_MCP_TOKEN",),
        ("AZURPILOT_GAME_LOCAL_MCP_TOKEN",),
    ]


def test_project_local_reader_fails_closed_when_selected_key_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    monkeypatch.setattr(
        local_http_auth,
        "read_local_environment_subset",
        lambda _path, *, keys: {},
    )

    with pytest.raises(local_http_auth.LocalHttpAuthError):
        local_http_auth.read_local_mcp_token(root, "azurpilot-game")


def test_project_local_reader_treats_empty_token_as_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    monkeypatch.setattr(
        local_http_auth,
        "read_local_environment_subset",
        lambda _path, *, keys: {keys[0]: ""},
    )

    with pytest.raises(local_http_auth.LocalHttpAuthError) as error:
        local_http_auth.read_local_mcp_token(root, "azurpilot-dev")

    assert not isinstance(error.value, local_http_auth.LocalHttpAuthUnknownError)


def test_project_local_reader_marks_unobservable_source_as_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)

    def unreadable_subset(*_args, **_kwargs):
        raise PermissionError("synthetic access denial")

    monkeypatch.setattr(
        local_http_auth, "read_local_environment_subset", unreadable_subset
    )

    with pytest.raises(local_http_auth.LocalHttpAuthUnknownError):
        local_http_auth.read_local_mcp_token(root, "azurpilot-dev")


def test_project_local_reader_preserves_unknown_storage_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    monkeypatch.setattr(
        local_http_auth,
        "read_local_environment_subset",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            StorageConfigurationUnknownError("read race")
        ),
    )

    with pytest.raises(local_http_auth.LocalHttpAuthUnknownError):
        local_http_auth.read_local_mcp_token(root, "azurpilot-dev")


def test_helper_emits_only_bounded_json_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setattr(
        local_http_auth,
        "read_local_mcp_token",
        lambda _root, _server: "project-token",
    )

    assert local_http_auth.main(["--server", "azurpilot-dev"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"Authorization": "Bearer project-token"}
    assert captured.err == ""


def test_helper_failure_does_not_echo_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setenv("AZURPILOT_DEV_LOCAL_MCP_TOKEN", "ambient-token")

    assert local_http_auth.main(["--server", "azurpilot-dev"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "LOCAL_MCP_AUTH_UNAVAILABLE"
    assert "ambient-token" not in captured.err


def test_helper_distinguishes_unknown_auth_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setattr(
        local_http_auth,
        "read_local_mcp_token",
        lambda *_args: (_ for _ in ()).throw(
            local_http_auth.LocalHttpAuthUnknownError("LOCAL_MCP_AUTH_UNKNOWN")
        ),
    )

    assert local_http_auth.main(["--server", "azurpilot-dev"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "LOCAL_MCP_AUTH_UNKNOWN"
