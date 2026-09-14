"""Контекст principal и transport текущего запроса для shared MCP transport."""

from __future__ import annotations

from contextvars import ContextVar, Token

from mcp.server.auth.provider import AccessToken

_CURRENT_ACCESS_TOKEN: ContextVar[AccessToken | None] = ContextVar(
    "mcp_current_access_token",
    default=None,
)
_CURRENT_TRANSPORT: ContextVar[str] = ContextVar(
    "mcp_current_transport",
    default="local_stdio",
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


def current_transport() -> str:
    """Вернуть transport текущего MCP request."""

    return _CURRENT_TRANSPORT.get()


def set_current_transport(transport: str) -> Token[str]:
    """Установить transport на время downstream request."""

    return _CURRENT_TRANSPORT.set(transport)


def reset_current_transport(token: Token[str]) -> None:
    """Восстановить предыдущий transport после downstream request."""

    _CURRENT_TRANSPORT.reset(token)


__all__ = (
    "current_access_token",
    "current_transport",
    "reset_current_access_token",
    "reset_current_transport",
    "set_current_access_token",
    "set_current_transport",
)
