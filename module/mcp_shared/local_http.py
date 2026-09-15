"""Защищённый loopback Streamable HTTP transport для локального Codex Desktop.

Локальный HTTP transport существует отдельно от public remote MCP. Он не
использует OAuth/OIDC и не меняет canonical server identity: это first-class
authenticated loopback route с той же backend implementation и local authority.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Self

from mcp.server.auth.provider import AccessToken
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from module.mcp_shared.auth import (
    reset_current_access_token,
    reset_current_transport,
    set_current_access_token,
    set_current_transport,
)
from module.mcp_shared.remote import (
    DEFAULT_BODY_READ_TIMEOUT_SECONDS,
    DEFAULT_CONCURRENCY_ACQUIRE_TIMEOUT_SECONDS,
    DEFAULT_MAX_CONCURRENT_REQUESTS,
    DEFAULT_MAX_REQUEST_BODY_BYTES,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ConcurrencyLimitMiddleware,
    FailSafeMiddleware,
    RequestBodyLimitMiddleware,
    RequestTimeoutMiddleware,
    _StreamableHTTPASGIApp,
)

logger = logging.getLogger(__name__)

MCP_PATH = "/mcp"
HEALTH_PATH = "/health"
READY_PATH = "/ready"
_TOKEN_ENV_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_SERVER_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,127}$")
LOCAL_HTTP_TRANSPORT = "local_http"


class LocalHttpConfigError(ValueError):
    """Конфигурация local HTTP MCP не соответствует fail-closed контракту."""


@dataclass(frozen=True, slots=True)
class LocalHttpConfig:
    """Bounded конфигурация одного loopback MCP HTTP endpoint."""

    server_name: str
    port: int
    required_scope: str
    token_env_var: str
    token: str = field(repr=False)
    accepted_scopes: tuple[str, ...] | None = None
    bind_host: str = "127.0.0.1"
    max_request_body_bytes: int = DEFAULT_MAX_REQUEST_BODY_BYTES
    max_concurrent_requests: int = DEFAULT_MAX_CONCURRENT_REQUESTS
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    body_read_timeout_seconds: float = DEFAULT_BODY_READ_TIMEOUT_SECONDS
    concurrency_acquire_timeout_seconds: float = (
        DEFAULT_CONCURRENCY_ACQUIRE_TIMEOUT_SECONDS
    )

    def __post_init__(self) -> None:
        if not isinstance(self.server_name, str) or not _SERVER_NAME_PATTERN.fullmatch(
            self.server_name
        ):
            raise LocalHttpConfigError("server_name local MCP имеет неверный формат")
        if self.bind_host != "127.0.0.1":
            raise LocalHttpConfigError(
                "Local MCP HTTP должен прослушивать только 127.0.0.1"
            )
        if (
            isinstance(self.port, bool)
            or not isinstance(self.port, int)
            or not (1 <= self.port <= 65535)
        ):
            raise LocalHttpConfigError("port local MCP имеет неверное значение")
        if (
            not isinstance(self.required_scope, str)
            or not self.required_scope
            or len(self.required_scope) > 128
            or any(character.isspace() for character in self.required_scope)
        ):
            raise LocalHttpConfigError("required_scope local MCP имеет неверный формат")
        if self.accepted_scopes is not None and (
            not self.accepted_scopes
            or any(
                not isinstance(scope, str)
                or not scope
                or len(scope) > 128
                or any(character.isspace() for character in scope)
                for scope in self.accepted_scopes
            )
        ):
            raise LocalHttpConfigError(
                "accepted_scopes local MCP имеет неверный формат"
            )
        if (
            self.accepted_scopes is not None
            and self.required_scope not in self.accepted_scopes
        ):
            raise LocalHttpConfigError(
                "accepted_scopes local MCP должен содержать required_scope"
            )
        if not isinstance(self.token_env_var, str) or not _TOKEN_ENV_PATTERN.fullmatch(
            self.token_env_var
        ):
            raise LocalHttpConfigError("token_env_var local MCP имеет неверный формат")
        if (
            not isinstance(self.token, str)
            or not self.token
            or len(self.token.encode("utf-8")) > 16 * 1024
            or any(character.isspace() for character in self.token)
        ):
            raise LocalHttpConfigError("Local MCP bearer token должен быть bounded")
        if not 1024 <= self.max_request_body_bytes <= 8 * 1024 * 1024:
            raise LocalHttpConfigError("max_request_body_bytes выходит за ограничение")
        if not 1 <= self.max_concurrent_requests <= 64:
            raise LocalHttpConfigError("max_concurrent_requests выходит за ограничение")
        for name, value, upper_bound in (
            ("request_timeout_seconds", self.request_timeout_seconds, 900.0),
            ("body_read_timeout_seconds", self.body_read_timeout_seconds, 60.0),
            (
                "concurrency_acquire_timeout_seconds",
                self.concurrency_acquire_timeout_seconds,
                30.0,
            ),
        ):
            if not 0 < value <= upper_bound:
                raise LocalHttpConfigError(f"{name} выходит за безопасное ограничение")

    @property
    def mcp_path(self) -> str:
        return MCP_PATH

    @property
    def public_host(self) -> str:
        return f"127.0.0.1:{self.port}"

    @property
    def url(self) -> str:
        return f"http://{self.public_host}{self.mcp_path}"

    @property
    def allowed_origins(self) -> tuple[str, ...]:
        return (f"http://{self.public_host}",)

    @classmethod
    def from_env(
        cls,
        *,
        server_name: str,
        port: int,
        required_scope: str,
        token_env_var: str,
        accepted_scopes: tuple[str, ...] | None = None,
    ) -> Self:
        """Собрать конфигурацию, не печатая и не сохраняя token в файлах."""

        token = os.environ.get(token_env_var, "")
        if not token:
            raise LocalHttpConfigError(
                f"Обязательная переменная {token_env_var} для local MCP не задана"
            )
        return cls(
            server_name=server_name,
            port=port,
            required_scope=required_scope,
            token_env_var=token_env_var,
            token=token,
            accepted_scopes=accepted_scopes,
        )


def _header_values(scope: Scope, name: str) -> list[str]:
    expected = name.lower().encode("ascii")
    return [
        value.decode("latin-1")
        for header_name, value in scope.get("headers", [])
        if header_name.lower() == expected
    ]


async def _send_error(
    send: Send,
    status_code: int,
    error: str,
    *,
    extra_headers: dict[str, str] | None = None,
) -> None:
    body = json.dumps({"error": error}, separators=(",", ":")).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"cache-control", b"no-store"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    for name, value in (extra_headers or {}).items():
        headers.append((name.lower().encode("ascii"), value.encode("latin-1")))
    await send(
        {"type": "http.response.start", "status": status_code, "headers": headers}
    )
    await send({"type": "http.response.body", "body": body})


class LocalStrictHostOriginMiddleware:
    """Разрешить только exact loopback Host и exact local Origin."""

    def __init__(self, app: ASGIApp, config: LocalHttpConfig) -> None:
        self.app = app
        self.expected_host = config.public_host.casefold()
        self.allowed_origins = frozenset(
            origin.casefold() for origin in config.allowed_origins
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        hosts = _header_values(scope, "host")
        if len(hosts) != 1 or hosts[0].casefold() != self.expected_host:
            await _send_error(send, 421, "invalid_host")
            return
        origins = _header_values(scope, "origin")
        if len(origins) > 1 or (
            origins and origins[0].casefold() not in self.allowed_origins
        ):
            await _send_error(send, 403, "invalid_origin")
            return
        await self.app(scope, receive, send)


class LocalBearerTokenMiddleware:
    """Проверить exact local bearer token перед MCP request."""

    def __init__(self, app: ASGIApp, config: LocalHttpConfig) -> None:
        self.app = app
        self.config = config

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or scope.get("path") != self.config.mcp_path:
            await self.app(scope, receive, send)
            return
        token = self._extract_token(scope)
        if token is None or not hmac.compare_digest(
            token.encode("utf-8"), self.config.token.encode("utf-8")
        ):
            await _send_error(
                send,
                401,
                "unauthorized",
                extra_headers={"WWW-Authenticate": "Bearer"},
            )
            return
        access_token = AccessToken(
            token="",
            client_id="local-codex",
            scopes=list(self.config.accepted_scopes or (self.config.required_scope,)),
            expires_at=None,
            resource=self.config.url,
        )
        child_scope = dict(scope)
        child_scope["azurpilot.access_token"] = access_token
        access_token_context = set_current_access_token(access_token)
        transport_context = set_current_transport(LOCAL_HTTP_TRANSPORT)
        try:
            await self.app(child_scope, receive, send)
        finally:
            reset_current_transport(transport_context)
            reset_current_access_token(access_token_context)

    @staticmethod
    def _extract_token(scope: Scope) -> str | None:
        values = _header_values(scope, "authorization")
        if len(values) != 1:
            return None
        scheme, separator, token = values[0].partition(" ")
        if (
            not separator
            or scheme.casefold() != "bearer"
            or not token
            or any(character.isspace() for character in token)
        ):
            return None
        if len(token.encode("utf-8")) > 16 * 1024:
            return None
        return token


def create_local_http_app(
    server_factory: Callable[..., Any],
    adapter: Any,
    *,
    config: LocalHttpConfig,
    identity_metadata: Mapping[str, object] | None = None,
) -> Starlette:
    """Создать stateless authenticated loopback Streamable HTTP app."""

    if adapter is None:
        raise ValueError("create_local_http_app требует заранее собранный adapter")
    server = server_factory(adapter, abandon_on_cancel=True)
    ready_metadata: dict[str, object] = {
        "server_name": config.server_name,
        "server_version": getattr(server, "version", None),
    }
    if identity_metadata is not None:
        for key in (
            "server_version",
            "source_revision",
            "source_set_digest",
            "tool_count",
            "tool_catalog_sha256",
            "capability_catalog_sha256",
            "contract_revision",
            "authorization_scopes",
            "dev_mcp_api_version",
            "game_mcp_api_version",
        ):
            value = identity_metadata.get(key)
            if isinstance(value, str) or (
                isinstance(value, int) and not isinstance(value, bool)
            ):
                ready_metadata[key] = value
            elif key == "authorization_scopes" and isinstance(value, (list, tuple)):
                scopes = list(value)
                if all(isinstance(scope, str) for scope in scopes):
                    ready_metadata[key] = scopes
    session_manager = StreamableHTTPSessionManager(
        app=server,
        json_response=True,
        stateless=True,
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[config.public_host],
            allowed_origins=list(config.allowed_origins),
        ),
    )

    async def health(_: Request) -> Response:
        return JSONResponse(
            {
                "ok": True,
                "code": "LOCAL_MCP_HEALTHY",
                "server_name": config.server_name,
                "transport": LOCAL_HTTP_TRANSPORT,
                **ready_metadata,
            },
            headers={"cache-control": "no-store"},
        )

    async def ready(_: Request) -> Response:
        return JSONResponse(
            {
                "ok": True,
                "code": "LOCAL_MCP_READY",
                "server_name": config.server_name,
                "transport": LOCAL_HTTP_TRANSPORT,
                **ready_metadata,
            },
            headers={"cache-control": "no-store"},
        )

    mcp_endpoint: ASGIApp = _StreamableHTTPASGIApp(session_manager)
    mcp_endpoint = RequestBodyLimitMiddleware(mcp_endpoint, config)  # type: ignore[arg-type]
    mcp_endpoint = LocalBearerTokenMiddleware(mcp_endpoint, config)

    @asynccontextmanager
    async def lifespan(_: Starlette):
        try:
            async with session_manager.run():
                yield
        finally:
            close = getattr(adapter, "close", None)
            if callable(close):
                close()

    app = Starlette(
        debug=False,
        middleware=[],
        routes=[
            Route(
                config.mcp_path,
                endpoint=mcp_endpoint,
                methods=["GET", "POST", "DELETE"],
            ),
            Route(HEALTH_PATH, endpoint=health, methods=["GET"]),
            Route(READY_PATH, endpoint=ready, methods=["GET"]),
        ],
        lifespan=lifespan,
    )
    app.add_middleware(FailSafeMiddleware)
    app.add_middleware(RequestTimeoutMiddleware, config=config)  # type: ignore[arg-type]
    app.add_middleware(LocalStrictHostOriginMiddleware, config=config)
    app.add_middleware(ConcurrencyLimitMiddleware, config=config)  # type: ignore[arg-type]
    app.state.local_http_config = config
    app.state.session_manager = session_manager
    return app


__all__ = (
    "HEALTH_PATH",
    "LOCAL_HTTP_TRANSPORT",
    "MCP_PATH",
    "READY_PATH",
    "LocalBearerTokenMiddleware",
    "LocalHttpConfig",
    "LocalHttpConfigError",
    "LocalStrictHostOriginMiddleware",
    "create_local_http_app",
)
