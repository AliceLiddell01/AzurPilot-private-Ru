"""Безопасный project-local источник bearer credential для MCP HTTP."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from azurpilot.tooling.mcp_filesystem import path_has_link
from module.application.errors import StorageConfigurationError
from module.persistence.local_environment import read_local_environment_subset

LOCAL_HTTP_TOKEN_ENV_VARS = {
    "azurpilot-dev": "AZURPILOT_DEV_LOCAL_MCP_TOKEN",
    "azurpilot-game": "AZURPILOT_GAME_LOCAL_MCP_TOKEN",
}
LOCAL_HTTP_ENDPOINTS = {
    "azurpilot-dev": "http://127.0.0.1:8775/mcp",
    "azurpilot-game": "http://127.0.0.1:8776/mcp",
}
MAX_LOCAL_HTTP_TOKEN_BYTES = 4096


class LocalHttpAuthError(RuntimeError):
    """Безопасность и доступность project-local credential нельзя подтвердить."""


def _repository_root_from_cwd() -> Path:
    """Найти только текущий project root, не принимая путь от вызывающего клиента."""

    current = Path.cwd().absolute()
    for candidate in (current, *current.parents):
        if (
            (candidate / "pyproject.toml").is_file()
            and (candidate / ".codex" / "config.toml").is_file()
            and not path_has_link(candidate)
        ):
            return candidate
    raise LocalHttpAuthError("LOCAL_MCP_AUTH_UNAVAILABLE")


def read_local_mcp_token(repository_root: str | Path, server_name: str) -> str:
    """Прочитать один MCP token из защищённого project-local ``.env``.

    Функция намеренно не имеет ambient-environment fallback: credentials
    принадлежат только exact repository root и закрытому server identity.
    """

    token_key = LOCAL_HTTP_TOKEN_ENV_VARS.get(server_name)
    if token_key is None:
        raise LocalHttpAuthError("LOCAL_MCP_AUTH_UNAVAILABLE")
    root = Path(repository_root).absolute()
    env_path = root / ".env"
    if (
        path_has_link(root)
        or path_has_link(env_path)
        or not (root / "pyproject.toml").is_file()
        or not (root / ".codex" / "config.toml").is_file()
    ):
        raise LocalHttpAuthError("LOCAL_MCP_AUTH_UNAVAILABLE")
    try:
        values = read_local_environment_subset(env_path, keys=(token_key,))
    except (OSError, StorageConfigurationError, UnicodeError) as exc:
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
    """Сформировать аутентифицированные HTTP-заголовки без записи credential в журнал."""

    return {"Authorization": f"Bearer {read_local_mcp_token(repository_root, server_name)}"}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="HTTP-заголовки MCP из project-local конфигурации AzurPilot"
    )
    parser.add_argument(
        "--server",
        choices=tuple(LOCAL_HTTP_TOKEN_ENV_VARS),
        required=True,
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Точка входа Codex ``http_headers_helper`` с JSON-only stdout."""

    args = _parse_args(argv)
    try:
        headers = local_http_headers(_repository_root_from_cwd(), args.server)
    except LocalHttpAuthError:
        print("LOCAL_MCP_AUTH_UNAVAILABLE", file=sys.stderr)
        return 1
    print(json.dumps(headers, ensure_ascii=False, separators=(",", ":")))
    return 0


__all__ = (
    "LOCAL_HTTP_ENDPOINTS",
    "LOCAL_HTTP_TOKEN_ENV_VARS",
    "LocalHttpAuthError",
    "local_http_headers",
    "main",
    "read_local_mcp_token",
)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
