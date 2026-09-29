"""Безопасный источник bearer-учётных данных MCP HTTP, привязанный к проекту."""

from __future__ import annotations

import argparse
import hmac
import json
import stat
import sys
from pathlib import Path

from azurpilot.tooling.mcp_filesystem import path_has_link
from module.application.errors import (
    StorageConfigurationError,
    StorageConfigurationUnknownError,
)
from module.mcp_shared.local_http_constants import LOCAL_HTTP_ENDPOINTS
from module.mcp_shared.windows_mcp_bridge_contract import (
    BRIDGE_CALLER_TOKEN_ENV_VAR,
)
from module.persistence.local_environment import read_local_environment_subset

LOCAL_HTTP_TOKEN_ENV_VARS = {
    "azurpilot-dev": "AZURPILOT_DEV_LOCAL_MCP_TOKEN",
    "azurpilot-game": "AZURPILOT_GAME_LOCAL_MCP_TOKEN",
}
MAX_LOCAL_HTTP_TOKEN_BYTES = 4096


class LocalHttpAuthError(RuntimeError):
    """Учётные данные проекта отсутствуют или имеют неверный формат."""


class LocalHttpAuthUnknownError(LocalHttpAuthError):
    """Состояние учётных данных проекта нельзя безопасно подтвердить."""


def _required_file_exists(path: Path) -> bool:
    try:
        metadata = path.stat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise LocalHttpAuthUnknownError("LOCAL_MCP_AUTH_UNKNOWN") from exc
    return stat.S_ISREG(metadata.st_mode)


def _repository_root_from_cwd() -> Path:
    """Найти только корень текущего проекта, не принимая путь от вызывающей стороны."""

    try:
        current = Path.cwd().absolute()
        for candidate in (current, *current.parents):
            if (
                _required_file_exists(candidate / "pyproject.toml")
                and _required_file_exists(candidate / ".codex" / "config.toml")
                and not path_has_link(candidate)
            ):
                return candidate
    except OSError as exc:
        raise LocalHttpAuthUnknownError("LOCAL_MCP_AUTH_UNKNOWN") from exc
    raise LocalHttpAuthError("LOCAL_MCP_AUTH_UNAVAILABLE")


def read_local_mcp_token(repository_root: str | Path, server_name: str) -> str:
    """Прочитать один токен MCP из защищённого файла ``.env`` проекта.

    Функция намеренно не использует переменные окружения процесса как запасной
    источник: учётные данные принадлежат только корню репозитория и заданной
    идентичности сервера.
    """

    token_key = LOCAL_HTTP_TOKEN_ENV_VARS.get(server_name)
    if token_key is None:
        raise LocalHttpAuthError("LOCAL_MCP_AUTH_UNAVAILABLE")
    return _read_project_local_token(repository_root, token_key)


def read_local_mcp_bridge_caller_token(repository_root: str | Path) -> str:
    """Прочитать отдельный токен клиента моста только из файла ``.env`` проекта."""

    token = _read_project_local_token(repository_root, BRIDGE_CALLER_TOKEN_ENV_VAR)
    for server_name in LOCAL_HTTP_TOKEN_ENV_VARS:
        try:
            internal_token = read_local_mcp_token(repository_root, server_name)
        except LocalHttpAuthUnknownError:
            raise
        except LocalHttpAuthError:
            continue
        if hmac.compare_digest(token.encode("utf-8"), internal_token.encode("utf-8")):
            raise LocalHttpAuthError("LOCAL_MCP_BRIDGE_CREDENTIAL_NOT_SEPARATE")
    return token


def _read_project_local_token(repository_root: str | Path, token_key: str) -> str:
    """Прочитать один зарегистрированный токен без запасного источника."""

    try:
        root = Path(repository_root).absolute()
        env_path = root / ".env"
        project_files_exist = _required_file_exists(root / "pyproject.toml") and (
            _required_file_exists(root / ".codex" / "config.toml")
        )
        unsafe_path = path_has_link(root) or path_has_link(env_path)
    except OSError as exc:
        raise LocalHttpAuthUnknownError("LOCAL_MCP_AUTH_UNKNOWN") from exc
    if unsafe_path or not project_files_exist:
        raise LocalHttpAuthError("LOCAL_MCP_AUTH_UNAVAILABLE")
    try:
        values = read_local_environment_subset(env_path, keys=(token_key,))
    except StorageConfigurationUnknownError as exc:
        raise LocalHttpAuthUnknownError("LOCAL_MCP_AUTH_UNKNOWN") from exc
    except OSError as exc:
        raise LocalHttpAuthUnknownError("LOCAL_MCP_AUTH_UNKNOWN") from exc
    except (StorageConfigurationError, UnicodeError) as exc:
        raise LocalHttpAuthError("LOCAL_MCP_AUTH_UNAVAILABLE") from exc
    token = values.get(token_key, "") if values is not None else ""
    if (
        not token
        or len(token.encode("utf-8")) > MAX_LOCAL_HTTP_TOKEN_BYTES
        or any(character.isspace() or ord(character) < 0x20 for character in token)
    ):
        raise LocalHttpAuthError("LOCAL_MCP_AUTH_UNAVAILABLE")
    return token


def local_http_headers(repository_root: str | Path, server_name: str) -> dict[str, str]:
    """Сформировать аутентифицированные HTTP-заголовки, не записывая учётные данные в журнал."""

    return {"Authorization": f"Bearer {read_local_mcp_token(repository_root, server_name)}"}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="HTTP-заголовки MCP из конфигурации проекта AzurPilot"
    )
    parser.add_argument(
        "--server",
        choices=tuple(LOCAL_HTTP_TOKEN_ENV_VARS),
        required=True,
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Точка входа Codex ``http_headers_helper`` с выводом только JSON в stdout."""

    args = _parse_args(argv)
    try:
        headers = local_http_headers(_repository_root_from_cwd(), args.server)
    except LocalHttpAuthUnknownError:
        print("LOCAL_MCP_AUTH_UNKNOWN", file=sys.stderr)
        return 1
    except LocalHttpAuthError:
        print("LOCAL_MCP_AUTH_UNAVAILABLE", file=sys.stderr)
        return 1
    print(json.dumps(headers, ensure_ascii=False, separators=(",", ":")))
    return 0


__all__ = (
    "LOCAL_HTTP_ENDPOINTS",
    "LOCAL_HTTP_TOKEN_ENV_VARS",
    "LocalHttpAuthError",
    "LocalHttpAuthUnknownError",
    "local_http_headers",
    "main",
    "read_local_mcp_bridge_caller_token",
    "read_local_mcp_token",
)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
