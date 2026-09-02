"""Контекст authenticated principal текущего запроса для shared MCP transport."""

from __future__ import annotations

from contextvars import ContextVar, Token

from mcp.server.auth.provider import AccessToken

_CURRENT_ACCESS_TOKEN: ContextVar[AccessToken | None] = ContextVar(
    "mcp_current_access_token",
    default=None,
)


def current_access_token() -> AccessToken | None:
    """Вернуть principal текущего MCP request или None для local authority."""

    return _CURRENT_ACCESS_TOKEN.get()


def set_current_access_token(access_token: AccessToken) -> Token[AccessToken | None]:
    """Установить principal на время downstream request."""

    return _CURRENT_ACCESS_TOKEN.set(access_token)


def reset_current_access_token(token: Token[AccessToken | None]) -> None:
    """Восстановить предыдущий request context."""

    _CURRENT_ACCESS_TOKEN.reset(token)


__all__ = (
    "current_access_token",
    "reset_current_access_token",
    "set_current_access_token",
)
