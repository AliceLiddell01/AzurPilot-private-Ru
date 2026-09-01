"""Общая bounded/authenticated инфраструктура Streamable HTTP MCP.

Модуль не знает о конкретном MCP-контракте. Сервер и scope передаются через
factory и аргументы, поэтому Dev и Game остаются отдельными security domains.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import time
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from threading import Lock
from typing import Any, Self
from urllib.parse import SplitResult, urlsplit

import anyio
import jwt
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

MCP_PATH = "/mcp"
DEFAULT_MAX_REQUEST_BODY_BYTES = 1024 * 1024
DEFAULT_MAX_CONCURRENT_REQUESTS = 8
DEFAULT_REQUEST_TIMEOUT_SECONDS = 180.0
DEFAULT_BODY_READ_TIMEOUT_SECONDS = 10.0
DEFAULT_CONCURRENCY_ACQUIRE_TIMEOUT_SECONDS = 2.0
DEFAULT_VERIFICATION_TIMEOUT_SECONDS = 5.0
DEFAULT_ALLOWED_ORIGINS = (
    "https://chatgpt.com",
    "https://chat.openai.com",
)
_JWT_ALGORITHMS = ("RS256",)


class RemoteConfigError(ValueError):
    """Конфигурация authenticated remote MCP не соответствует контракту."""


def _header_values(scope: Scope, name: str) -> list[str]:
    expected = name.lower().encode("ascii")
    return [
        value.decode("latin-1")
        for header_name, value in scope.get("headers", [])
        if header_name.lower() == expected
    ]


def _parse_https_url(
    name: str,
    value: str,
    *,
    exact_path: str | None = None,
) -> SplitResult:
    if not isinstance(value, str) or not value:
        raise RemoteConfigError(f"{name}: значение обязательно")
    if not value.isascii():
        raise RemoteConfigError(f"{name}: URL должен содержать только ASCII")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise RemoteConfigError(f"{name}: некорректный URL") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (exact_path is not None and parsed.path != exact_path)
    ):
        raise RemoteConfigError(
            f"{name}: требуется HTTPS URL без credentials и fragment"
        )
    if exact_path is not None and port is not None:
        raise RemoteConfigError(
            f"{name}: требуется публичный HTTPS-порт через reverse proxy"
        )
    return parsed


def _validate_origin(value: str) -> None:
    if not isinstance(value, str) or not value.isascii():
        raise RemoteConfigError("Разрешённые Origin должны содержать только ASCII")
    try:
        parsed = urlsplit(value)
        parsed_port = parsed.port
    except ValueError as exc:
        raise RemoteConfigError(
            "Разрешённые Origin должны быть корректными URL"
        ) from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or "*" in value
    ):
        raise RemoteConfigError(
            "Разрешённые Origin должны быть точными HTTPS origin без подстановочных символов"
        )
    if parsed_port is not None and not 1 <= parsed_port <= 65535:
        raise RemoteConfigError(
            "Разрешённые Origin должны использовать корректный порт"
        )


def _canonical_host(parsed: SplitResult) -> str:
    hostname = parsed.hostname
    if hostname is None:
        raise RemoteConfigError("public_url: отсутствует hostname")
    host = hostname.casefold()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return host


@dataclass(frozen=True, slots=True)
class RemoteConfig:
    """Fail-closed конфигурация публичного resource server."""

    public_url: str
    oauth_issuer: str
    oauth_audience: str
    oauth_jwks_url: str
    oauth_subject: str
    bind_host: str = "127.0.0.1"
    allowed_origins: tuple[str, ...] = DEFAULT_ALLOWED_ORIGINS
    max_request_body_bytes: int = DEFAULT_MAX_REQUEST_BODY_BYTES
    max_concurrent_requests: int = DEFAULT_MAX_CONCURRENT_REQUESTS
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    body_read_timeout_seconds: float = DEFAULT_BODY_READ_TIMEOUT_SECONDS
    concurrency_acquire_timeout_seconds: float = (
        DEFAULT_CONCURRENCY_ACQUIRE_TIMEOUT_SECONDS
    )
    token_verification_timeout_seconds: float = DEFAULT_VERIFICATION_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if self.bind_host != "127.0.0.1":
            raise RemoteConfigError(
                "Backend remote MCP должен прослушивать только 127.0.0.1"
            )
        _parse_https_url("public_url", self.public_url, exact_path=MCP_PATH)
        _parse_https_url("oauth_issuer", self.oauth_issuer)
        _parse_https_url("oauth_jwks_url", self.oauth_jwks_url)
        if not self.oauth_audience or len(self.oauth_audience) > 512:
            raise RemoteConfigError(
                "oauth_audience должен быть непустым ограниченным значением"
            )
        if not self.oauth_subject or len(self.oauth_subject) > 256:
            raise RemoteConfigError(
                "oauth_subject должен быть непустым ограниченным значением"
            )
        if not self.allowed_origins:
            raise RemoteConfigError("Нужен хотя бы один точный разрешённый Origin")
        for origin in self.allowed_origins:
            _validate_origin(origin)
        if not 1024 <= self.max_request_body_bytes <= 8 * 1024 * 1024:
            raise RemoteConfigError(
                "max_request_body_bytes выходит за безопасное ограничение"
            )
        if not 1 <= self.max_concurrent_requests <= 64:
            raise RemoteConfigError(
                "max_concurrent_requests выходит за безопасное ограничение"
            )
        for name, value, upper_bound in (
            ("request_timeout_seconds", self.request_timeout_seconds, 900.0),
            ("body_read_timeout_seconds", self.body_read_timeout_seconds, 60.0),
            (
                "concurrency_acquire_timeout_seconds",
                self.concurrency_acquire_timeout_seconds,
                30.0,
            ),
            (
                "token_verification_timeout_seconds",
                self.token_verification_timeout_seconds,
                30.0,
            ),
        ):
            if not 0 < value <= upper_bound:
                raise RemoteConfigError(f"{name} выходит за безопасное ограничение")

    @property
    def mcp_path(self) -> str:
        return MCP_PATH

    @property
    def public_host(self) -> str:
        return _canonical_host(urlsplit(self.public_url))

    @property
    def resource_metadata_path(self) -> str:
        return f"/.well-known/oauth-protected-resource{self.mcp_path}"

    @property
    def resource_metadata_url(self) -> str:
        parsed = urlsplit(self.public_url)
        return f"{parsed.scheme}://{parsed.netloc}{self.resource_metadata_path}"

    @classmethod
    def from_env(cls, prefix: str) -> Self:
        if not isinstance(prefix, str) or not prefix or not prefix.isascii():
            raise RemoteConfigError("Префикс remote MCP environment некорректен")

        def required(suffix: str) -> str:
            name = f"{prefix}_{suffix}"
            value = os.environ.get(name, "").strip()
            if not value:
                raise RemoteConfigError(f"Обязательная переменная {name} не задана")
            return value

        raw_origins = os.environ.get(f"{prefix}_ALLOWED_ORIGINS")
        origins = (
            DEFAULT_ALLOWED_ORIGINS
            if raw_origins is None
            else tuple(
                value.strip() for value in raw_origins.split(",") if value.strip()
            )
        )
        return cls(
            public_url=required("PUBLIC_URL"),
            oauth_issuer=required("OAUTH_ISSUER"),
            oauth_audience=required("OAUTH_AUDIENCE"),
            oauth_jwks_url=required("OAUTH_JWKS_URL"),
            oauth_subject=required("OAUTH_SUBJECT"),
            allowed_origins=origins,
        )


class OIDCTokenVerifier:
    """Проверить подписанный OAuth/OIDC access token для одного resource."""

    def __init__(
        self,
        config: RemoteConfig,
        *,
        required_scope: str,
        jwk_client: Any | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._required_scope = required_scope
        self._clock = clock
        self._jwk_client = jwk_client or PyJWKClient(
            config.oauth_jwks_url,
            cache_keys=True,
            cache_jwk_set=True,
            lifespan=300,
            timeout=config.token_verification_timeout_seconds,
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        if (
            not isinstance(token, str)
            or not token
            or len(token.encode("utf-8")) > 16 * 1024
        ):
            return None
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") not in _JWT_ALGORITHMS:
                return None
            with anyio.fail_after(self._config.token_verification_timeout_seconds):
                signing_key = await anyio.to_thread.run_sync(
                    self._jwk_client.get_signing_key_from_jwt,
                    token,
                    abandon_on_cancel=True,
                )
                claims = await anyio.to_thread.run_sync(
                    lambda: jwt.decode(
                        token,
                        signing_key.key,
                        algorithms=list(_JWT_ALGORITHMS),
                        audience=self._config.oauth_audience,
                        issuer=self._config.oauth_issuer,
                        options={"require": ["aud", "exp", "iss", "sub"]},
                        leeway=0,
                    ),
                    abandon_on_cancel=True,
                )
        except TimeoutError:
            logger.debug("Проверка OIDC-токена превысила ограничение времени")
            return None
        except (
            jwt.PyJWTError,
            OSError,
            ValueError,
            TypeError,
            AttributeError,
            RuntimeError,
        ) as exc:
            logger.debug("OIDC-токен отклонён при проверке: %s", type(exc).__name__)
            return None

        if not isinstance(claims, Mapping):
            return None
        subject = claims.get("sub")
        if not isinstance(subject, str) or not hmac.compare_digest(
            subject.encode("utf-8"), self._config.oauth_subject.encode("utf-8")
        ):
            return None
        resource = claims.get("resource")
        if not isinstance(resource, str) or not hmac.compare_digest(
            resource.encode("utf-8"), self._config.public_url.encode("utf-8")
        ):
            return None
        expires_at = claims.get("exp")
        if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
            return None
        expires_at_int = int(expires_at)
        if expires_at_int <= int(self._clock()):
            return None
        scopes = self._scopes(claims.get("scope"))
        if scopes is None or self._required_scope not in scopes:
            return None
        return AccessToken(
            token="",
            client_id=subject,
            scopes=scopes,
            expires_at=expires_at_int,
            resource=self._config.public_url,
        )

    @staticmethod
    def _scopes(value: object) -> list[str] | None:
        if isinstance(value, str):
            scopes = value.split()
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            scopes = value
        else:
            return None
        if any(not item or len(item) > 128 for item in scopes):
            return None
        return list(dict.fromkeys(scopes))


async def _send_error(
    send: Send,
    status_code: int,
    error: str,
    *,
    extra_headers: Mapping[str, str] | None = None,
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


class StrictHostOriginMiddleware:
    """Проверить только канонический Host и точные разрешённые Origin."""

    def __init__(self, app: ASGIApp, config: RemoteConfig) -> None:
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


class OAuthBearerMiddleware:
    """Проверить Bearer access token до запуска любого MCP request."""

    def __init__(
        self,
        app: ASGIApp,
        config: RemoteConfig,
        verifier: TokenVerifier,
        *,
        required_scope: str,
    ) -> None:
        self.app = app
        self.config = config
        self.verifier = verifier
        self.required_scope = required_scope

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or scope.get("path") != self.config.mcp_path:
            await self.app(scope, receive, send)
            return
        token = self._extract_token(scope)
        if token is None:
            await self._unauthorized(send)
            return
        try:
            access_token = await self.verifier.verify_token(token)
        except Exception as exc:  # noqa: BLE001 - auth boundary is fail-closed.
            logger.debug("Ошибка проверки Bearer-токена: %s", type(exc).__name__)
            access_token = None
        if access_token is None or (
            access_token.expires_at is not None
            and access_token.expires_at <= int(time.time())
        ):
            await self._unauthorized(send)
            return
        if self.required_scope not in access_token.scopes:
            await _send_error(
                send,
                403,
                "forbidden",
                extra_headers={
                    "WWW-Authenticate": self._challenge("insufficient_scope")
                },
            )
            return
        child_scope = dict(scope)
        child_scope["azurpilot.access_token"] = access_token
        await self.app(child_scope, receive, send)

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

    def _challenge(self, error: str | None = None) -> str:
        parts = [f'resource_metadata="{self.config.resource_metadata_url}"']
        if error:
            parts.append(f'error="{error}"')
        parts.append(f'scope="{self.required_scope}"')
        return f"Bearer {', '.join(parts)}"

    async def _unauthorized(self, send: Send) -> None:
        await _send_error(
            send,
            401,
            "unauthorized",
            extra_headers={"WWW-Authenticate": self._challenge()},
        )


class ConcurrencyLimitMiddleware:
    """Ограничить число одновременно обрабатываемых HTTP запросов."""

    def __init__(self, app: ASGIApp, config: RemoteConfig) -> None:
        self.app = app
        self._total_tokens = config.max_concurrent_requests
        self._limiter: anyio.CapacityLimiter | None = None
        self._limiter_lock = Lock()
        self._acquire_timeout = config.concurrency_acquire_timeout_seconds

    def _ensure_limiter(self) -> anyio.CapacityLimiter:
        with self._limiter_lock:
            if self._limiter is None:
                self._limiter = anyio.CapacityLimiter(self._total_tokens)
            return self._limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            self._ensure_limiter()
            await self.app(scope, receive, send)
            return
        limiter = self._ensure_limiter()
        acquired = False
        try:
            with anyio.fail_after(self._acquire_timeout):
                await limiter.acquire()
            acquired = True
        except TimeoutError:
            await _send_error(send, 503, "server_busy")
            return
        try:
            await self.app(scope, receive, send)
        finally:
            if acquired:
                limiter.release()


class RequestBodyLimitMiddleware:
    """Буферизовать и ограничить HTTP body до передачи его MCP SDK."""

    def __init__(self, app: ASGIApp, config: RemoteConfig) -> None:
        self.app = app
        self.mcp_path = config.mcp_path
        self.max_body_bytes = config.max_request_body_bytes
        self.read_timeout = config.body_read_timeout_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or scope.get("path") != self.mcp_path:
            await self.app(scope, receive, send)
            return
        content_lengths = _header_values(scope, "content-length")
        if len(content_lengths) > 1:
            await _send_error(send, 400, "bad_request")
            return
        if content_lengths:
            try:
                content_length = int(content_lengths[0])
            except ValueError:
                await _send_error(send, 400, "bad_request")
                return
            if content_length < 0 or content_length > self.max_body_bytes:
                await _send_error(send, 413, "request_too_large")
                return
        if scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        body = bytearray()
        try:
            with anyio.fail_after(self.read_timeout):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        await _send_error(send, 400, "bad_request")
                        return
                    if message["type"] != "http.request":
                        await _send_error(send, 400, "bad_request")
                        return
                    body.extend(message.get("body", b""))
                    if len(body) > self.max_body_bytes:
                        await _send_error(send, 413, "request_too_large")
                        return
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            await _send_error(send, 408, "request_timeout")
            return

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if replayed:
                return {"type": "http.disconnect"}
            replayed = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay, send)


class RequestTimeoutMiddleware:
    """Ограничить время MCP request без утечки traceback клиенту."""

    def __init__(self, app: ASGIApp, config: RemoteConfig) -> None:
        self.app = app
        self.timeout = config.request_timeout_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        response_started = False
        response_completed = False

        async def guarded_send(message: Message) -> None:
            nonlocal response_started, response_completed
            if message["type"] == "http.response.start":
                response_started = True
            elif message["type"] == "http.response.body" and not message.get(
                "more_body", False
            ):
                response_completed = True
            await send(message)

        try:
            with anyio.fail_after(self.timeout):
                await self.app(scope, receive, guarded_send)
        except TimeoutError:
            if not response_started:
                await _send_error(send, 504, "request_timeout")
            elif not response_completed:
                logger.warning("Истёк timeout remote MCP после начала HTTP-ответа")
                await send(
                    {"type": "http.response.body", "body": b"", "more_body": False}
                )


class FailSafeMiddleware:
    """Вернуть безопасную generic ошибку вместо необработанного traceback."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        response_started = False

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, guarded_send)
        except Exception as exc:  # noqa: BLE001 - HTTP boundary hides details.
            logger.error("Ошибка обработки remote MCP-запроса: %s", type(exc).__name__)
            if not response_started:
                await _send_error(send, 500, "server_error")


class _StreamableHTTPASGIApp:
    def __init__(self, session_manager: StreamableHTTPSessionManager) -> None:
        self.session_manager = session_manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.session_manager.handle_request(scope, receive, send)


def _metadata_headers(request: Request, config: RemoteConfig) -> dict[str, str]:
    origin = request.headers.get("origin")
    headers = {"cache-control": "no-store", "vary": "Origin"}
    allowed = {item.casefold() for item in config.allowed_origins}
    if origin is not None and origin.casefold() in allowed:
        headers.update(
            {
                "access-control-allow-origin": origin,
                "access-control-allow-methods": "GET, OPTIONS",
                "access-control-allow-headers": "Authorization, Content-Type, MCP-Protocol-Version",
                "access-control-max-age": "300",
            }
        )
    return headers


def create_remote_app(
    server_factory: Callable[..., Any],
    adapter: Any,
    *,
    config: RemoteConfig,
    token_verifier: TokenVerifier | None,
    required_scope: str,
) -> Starlette:
    """Создать stateless authenticated Streamable HTTP ASGI app."""

    if adapter is None:
        raise ValueError("create_remote_app требует заранее собранный adapter")
    verifier = token_verifier or OIDCTokenVerifier(
        config,
        required_scope=required_scope,
    )
    server = server_factory(adapter, abandon_on_cancel=True)
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

    async def metadata(request: Request) -> Response:
        hosts = _header_values(request.scope, "host")
        if len(hosts) != 1 or hosts[0].casefold() != config.public_host.casefold():
            return JSONResponse({"error": "invalid_host"}, status_code=421)
        origins = _header_values(request.scope, "origin")
        if len(origins) > 1 or (
            origins
            and origins[0].casefold()
            not in {item.casefold() for item in config.allowed_origins}
        ):
            return JSONResponse({"error": "invalid_origin"}, status_code=403)
        if request.method == "OPTIONS":
            return Response(status_code=204, headers=_metadata_headers(request, config))
        return JSONResponse(
            {
                "resource": config.public_url,
                "authorization_servers": [config.oauth_issuer],
                "scopes_supported": [required_scope],
                "bearer_methods_supported": ["header"],
            },
            headers=_metadata_headers(request, config),
        )

    mcp_endpoint: ASGIApp = _StreamableHTTPASGIApp(session_manager)
    mcp_endpoint = RequestBodyLimitMiddleware(mcp_endpoint, config)
    mcp_endpoint = OAuthBearerMiddleware(
        mcp_endpoint,
        config,
        verifier,
        required_scope=required_scope,
    )

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
        middleware=[
            Middleware(FailSafeMiddleware),
            Middleware(RequestTimeoutMiddleware, config=config),
            Middleware(StrictHostOriginMiddleware, config=config),
            Middleware(ConcurrencyLimitMiddleware, config=config),
        ],
        routes=[
            Route(
                config.mcp_path,
                endpoint=mcp_endpoint,
                methods=["GET", "POST", "DELETE"],
            ),
            Route(
                config.resource_metadata_path,
                endpoint=metadata,
                methods=["GET", "OPTIONS"],
            ),
        ],
        lifespan=lifespan,
    )
    app.state.remote_config = config
    app.state.session_manager = session_manager
    return app


__all__ = (
    "DEFAULT_ALLOWED_ORIGINS",
    "DEFAULT_BODY_READ_TIMEOUT_SECONDS",
    "DEFAULT_CONCURRENCY_ACQUIRE_TIMEOUT_SECONDS",
    "DEFAULT_MAX_CONCURRENT_REQUESTS",
    "DEFAULT_MAX_REQUEST_BODY_BYTES",
    "DEFAULT_REQUEST_TIMEOUT_SECONDS",
    "DEFAULT_VERIFICATION_TIMEOUT_SECONDS",
    "MCP_PATH",
    "ConcurrencyLimitMiddleware",
    "FailSafeMiddleware",
    "OAuthBearerMiddleware",
    "OIDCTokenVerifier",
    "RemoteConfig",
    "RemoteConfigError",
    "RequestBodyLimitMiddleware",
    "RequestTimeoutMiddleware",
    "StrictHostOriginMiddleware",
    "create_remote_app",
)
