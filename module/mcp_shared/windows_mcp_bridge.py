"""Ограниченный аутентифицированный мост Windows к принадлежащим проекту Dev/Game MCP."""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import re
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

import anyio
import httpx2 as httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from module.mcp_shared.local_http_auth import (
    LocalHttpAuthError,
    LocalHttpAuthUnknownError,
    read_local_mcp_bridge_caller_token,
    read_local_mcp_token,
)
from module.mcp_shared.local_http_supervisor import (
    LOCAL_HTTP_SERVICES,
    LocalHttpSupervisor,
    LocalHttpSupervisorError,
)
from module.mcp_shared.windows_mcp_bridge_contract import (
    BRIDGE_BACKEND_IDENTITY_HEADER,
    BRIDGE_BIND_HOST,
    BRIDGE_BODY_READ_TIMEOUT_SECONDS,
    BRIDGE_CONCURRENCY_TIMEOUT_SECONDS,
    BRIDGE_EXPECTED_IDENTITY_HEADER,
    BRIDGE_MAX_CONCURRENT_REQUESTS,
    BRIDGE_MAX_MCP_PARAM_HEADERS,
    BRIDGE_MAX_MCP_PARAM_HEADER_NAME_BYTES,
    BRIDGE_MAX_MCP_PARAM_HEADER_VALUE_BYTES,
    BRIDGE_MAX_MCP_PARAM_HEADERS_BYTES,
    BRIDGE_MAX_REQUEST_BODY_BYTES,
    BRIDGE_NAME,
    BRIDGE_PORT,
    BRIDGE_REQUEST_TIMEOUT_SECONDS,
    BRIDGE_ROUTES,
    BRIDGE_ROUTES_BY_PATH,
    BRIDGE_STREAM_IDLE_TIMEOUT_SECONDS,
    BridgeIdentityError,
    BridgeRoute,
    BridgeSourceIdentity,
    identity_from_ready_payload,
    identity_mismatch_fields,
    parse_expected_identity,
    serialize_identity,
)

logger = logging.getLogger(__name__)
_EXPECTED_HOST = f"{BRIDGE_BIND_HOST}:{BRIDGE_PORT}"
_EXPECTED_ORIGIN = f"http://{_EXPECTED_HOST}"
_MCP_HEADER_ALLOWLIST = (
    "accept-encoding",
    "accept",
    "content-type",
    "mcp-session-id",
    "mcp-protocol-version",
    "mcp-method",
    "mcp-name",
    "last-event-id",
)
_MCP_PARAM_HEADER_PREFIX = b"mcp-param-"
_MCP_PARAM_HEADER_TOKEN_CHARS = frozenset("!#$%&'*+-.^_`|~")
_RESPONSE_HEADER_ALLOWLIST = (
    "cache-control",
    "content-length",
    "content-encoding",
    "content-type",
    "mcp-protocol-version",
    "mcp-session-id",
)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class BridgeUpstreamObservation:
    """Безопасное состояние одной принадлежащей проекту серверной части MCP."""

    status: str
    reason_code: str
    identity: BridgeSourceIdentity | None = None


class BridgeRequestError(ValueError):
    def __init__(self, code: str, status_code: int) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def _supports_windows_mcp_bridge() -> bool:
    return os.name == "nt"


class _ClosingStreamingResponse(StreamingResponse):
    """Закрыть серверное соединение даже при ошибке отправки заголовков или тела."""

    def __init__(self, *args: Any, upstream: httpx.Response, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._upstream = upstream

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._upstream.aclose()


def _header_values(scope: Scope, name: str) -> list[str]:
    expected = name.lower().encode("ascii")
    return [
        value.decode("latin-1")
        for key, value in scope.get("headers", [])
        if key.lower() == expected
    ]


def _json_error(
    status_code: int,
    code: str,
    *,
    fields: tuple[str, ...] = (),
    expected: BridgeSourceIdentity | None = None,
    actual: BridgeSourceIdentity | None = None,
) -> JSONResponse:
    error: dict[str, object] = {"code": code}
    if fields:
        error["mismatched_fields"] = list(fields)
    if expected is not None:
        error["expected"] = expected.model_dump(mode="json")
    if actual is not None:
        error["actual"] = actual.model_dump(mode="json")
    return JSONResponse(
        {"error": error},
        status_code=status_code,
        headers={"cache-control": "no-store"},
    )


def _method_not_allowed(allowed: tuple[str, ...]) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": "BRIDGE_METHOD_NOT_ALLOWED"}},
        status_code=405,
        headers={
            "allow": ", ".join(allowed),
            "cache-control": "no-store",
        },
    )


def _bridge_route_status(
    route: BridgeRoute, repository_root: Path | str | None = None
) -> BridgeUpstreamObservation:
    """Прочитать сведения о готовности от подтверждённой серверной части."""

    service = next(
        (item for item in LOCAL_HTTP_SERVICES if item.name == route.server_name),
        None,
    )
    if service is None:  # pragma: no cover - закрытая таблица маршрутов моста
        return BridgeUpstreamObservation("unknown", "BRIDGE_UPSTREAM_UNKNOWN")
    try:
        supervisor = LocalHttpSupervisor(
            repository_root or Path.cwd(),
            services=(service,),
            state_namespace=service.name,
            create_state_directory=False,
        )
        status = supervisor.status()
        code = status.get("code")
        if code == "LOCAL_MCP_SUPERVISOR_STOPPED":
            if supervisor.port_conflicts():
                return BridgeUpstreamObservation("conflict", "BRIDGE_UPSTREAM_CONFLICT")
            return BridgeUpstreamObservation("stopped", "BRIDGE_UPSTREAM_STOPPED")
        if code == "LOCAL_MCP_SUPERVISOR_UNKNOWN":
            return BridgeUpstreamObservation("unknown", "BRIDGE_UPSTREAM_UNKNOWN")
        if code == "LOCAL_MCP_SUPERVISOR_OWNERSHIP_MISMATCH":
            return BridgeUpstreamObservation("conflict", "BRIDGE_UPSTREAM_CONFLICT")
        if code != "LOCAL_MCP_SUPERVISOR_READY":
            return BridgeUpstreamObservation("stale", "BRIDGE_UPSTREAM_STALE")
        services = status.get("services")
        item = (
            next(
                (
                    value
                    for value in services
                    if isinstance(value, dict)
                    and value.get("server_name") == route.server_name
                ),
                None,
            )
            if isinstance(services, list)
            else None
        )
        if (
            item is None
            or item.get("alive") is not True
            or item.get("ready") is not True
        ):
            return BridgeUpstreamObservation(
                "unavailable", "BRIDGE_UPSTREAM_UNAVAILABLE"
            )
        identity = identity_from_ready_payload(item)
        if identity.server_name != route.server_name:
            return BridgeUpstreamObservation(
                "unknown", "BRIDGE_UPSTREAM_IDENTITY_UNKNOWN"
            )
        return BridgeUpstreamObservation("ready", "BRIDGE_UPSTREAM_READY", identity)
    except BridgeIdentityError:
        return BridgeUpstreamObservation("unknown", "BRIDGE_UPSTREAM_IDENTITY_UNKNOWN")
    except OSError, LocalHttpSupervisorError, ValueError, TypeError:
        return BridgeUpstreamObservation("unknown", "BRIDGE_UPSTREAM_UNKNOWN")


class BridgeHostOriginMiddleware:
    """Ограничить мост точными значениями Host и Origin для loopback."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        hosts = _header_values(scope, "host")
        origins = _header_values(scope, "origin")
        if len(hosts) != 1 or hosts[0].casefold() != _EXPECTED_HOST.casefold():
            await _send_json(send, 421, "BRIDGE_INVALID_HOST")
            return
        if len(origins) > 1 or (
            origins and origins[0].casefold() != _EXPECTED_ORIGIN.casefold()
        ):
            await _send_json(send, 403, "BRIDGE_INVALID_ORIGIN")
            return
        await self.app(scope, receive, send)


class BridgeCallerAuthMiddleware:
    """Проверить отдельный токен вызывающего клиента до маршрутизации и диагностики."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        repository_root: Path,
        token_reader: Callable[[str | Path], str] = read_local_mcp_bridge_caller_token,
    ) -> None:
        self.app = app
        self.repository_root = repository_root
        self.token_reader = token_reader

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path")
        if path in {"/health", "/ready"} and scope.get("method") == "GET":
            await self.app(scope, receive, send)
            return
        values = _header_values(scope, "authorization")
        if len(values) != 1:
            await _send_json(
                send,
                401,
                "BRIDGE_CALLER_AUTH_INVALID",
                extra_headers={"www-authenticate": "Bearer"},
            )
            return
        scheme, separator, caller_token = values[0].partition(" ")
        if (
            not separator
            or scheme.casefold() != "bearer"
            or not caller_token
            or len(caller_token.encode("utf-8")) > 4096
            or any(character.isspace() for character in caller_token)
        ):
            await _send_json(
                send,
                401,
                "BRIDGE_CALLER_AUTH_INVALID",
                extra_headers={"www-authenticate": "Bearer"},
            )
            return
        try:
            expected = await anyio.to_thread.run_sync(
                self.token_reader, self.repository_root
            )
        except LocalHttpAuthUnknownError:
            await _send_json(send, 503, "BRIDGE_CALLER_AUTH_UNKNOWN")
            return
        except LocalHttpAuthError, OSError:
            await _send_json(send, 503, "BRIDGE_CALLER_AUTH_UNAVAILABLE")
            return
        if not hmac.compare_digest(caller_token.encode(), expected.encode()):
            await _send_json(
                send,
                401,
                "BRIDGE_CALLER_AUTH_INVALID",
                extra_headers={"www-authenticate": "Bearer"},
            )
            return
        await self.app(scope, receive, send)


class BridgeConcurrencyMiddleware:
    """Ограничить число запросов и удерживать разрешение до завершения потока SSE."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._limiter: anyio.CapacityLimiter | None = None
        self._lock = Lock()

    def _get_limiter(self) -> anyio.CapacityLimiter:
        with self._lock:
            if self._limiter is None:
                self._limiter = anyio.CapacityLimiter(BRIDGE_MAX_CONCURRENT_REQUESTS)
            return self._limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        limiter = self._get_limiter()
        acquired = False
        try:
            with anyio.fail_after(BRIDGE_CONCURRENCY_TIMEOUT_SECONDS):
                await limiter.acquire()
                acquired = True
        except TimeoutError:
            await _send_json(send, 503, "BRIDGE_SERVER_BUSY")
            return
        try:
            await self.app(scope, receive, send)
        finally:
            if acquired:
                limiter.release()


class BridgeTimeoutMiddleware:
    """Сохранять долгие потоки MCP GET и ограничивать время остальных HTTP-операций."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("method") == "GET" and scope.get("path") in BRIDGE_ROUTES_BY_PATH:
            async def bounded_stream_send(message: Message) -> None:
                with anyio.fail_after(BRIDGE_STREAM_IDLE_TIMEOUT_SECONDS):
                    await send(message)

            await self.app(scope, receive, bounded_stream_send)
            return
        response_started = False
        response_completed = False

        async def tracked_send(message: Message) -> None:
            nonlocal response_started, response_completed
            if message["type"] == "http.response.start":
                response_started = True
            elif message["type"] == "http.response.body" and not message.get(
                "more_body", False
            ):
                response_completed = True
            await send(message)

        try:
            with anyio.fail_after(BRIDGE_REQUEST_TIMEOUT_SECONDS):
                await self.app(scope, receive, tracked_send)
        except TimeoutError:
            if not response_started:
                await _send_json(send, 504, "BRIDGE_REQUEST_TIMEOUT")
            elif not response_completed:
                # Не отправлять второй response.start после начала потока MCP.
                await send({"type": "http.response.body", "body": b""})


class BridgeFailSafeMiddleware:
    """Не передавать наружу traceback или произвольное содержимое исключения."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        response_started = False

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, guarded_send)
        except Exception as exc:  # noqa: BLE001 — HTTP-граница скрывает исходные сведения.
            logger.error("Ошибка обработки моста Windows MCP: %s", type(exc).__name__)
            if not response_started:
                await _send_json(send, 500, "BRIDGE_INTERNAL_ERROR")


async def _send_json(
    send: Send,
    status_code: int,
    code: str,
    *,
    extra_headers: Mapping[str, str] | None = None,
) -> None:
    headers = [(b"content-type", b"application/json"), (b"cache-control", b"no-store")]
    if extra_headers:
        headers.extend(
            (name.lower().encode("ascii"), value.encode("latin-1"))
            for name, value in extra_headers.items()
        )
    body = json.dumps({"error": {"code": code}}, separators=(",", ":")).encode()
    await send(
        {"type": "http.response.start", "status": status_code, "headers": headers}
    )
    await send({"type": "http.response.body", "body": body})


async def _read_bounded_body(request: Request) -> bytes:
    lengths = _header_values(request.scope, "content-length")
    transfer_encodings = _header_values(request.scope, "transfer-encoding")
    if (
        len(lengths) > 1
        or len(transfer_encodings) > 1
        or (lengths and transfer_encodings)
    ):
        raise BridgeRequestError("BRIDGE_REQUEST_INVALID", 400)
    if transfer_encodings and transfer_encodings[0].casefold() != "chunked":
        raise BridgeRequestError("BRIDGE_REQUEST_INVALID", 400)
    if lengths:
        try:
            content_length = int(lengths[0])
        except ValueError as exc:
            raise BridgeRequestError("BRIDGE_REQUEST_INVALID", 400) from exc
        if content_length < 0:
            raise BridgeRequestError("BRIDGE_REQUEST_INVALID", 400)
        if content_length > BRIDGE_MAX_REQUEST_BODY_BYTES:
            raise BridgeRequestError("BRIDGE_REQUEST_TOO_LARGE", 413)
    body = bytearray()
    try:
        with anyio.fail_after(BRIDGE_BODY_READ_TIMEOUT_SECONDS):
            while True:
                message = await request.receive()
                if message["type"] == "http.disconnect":
                    raise BridgeRequestError("BRIDGE_REQUEST_INVALID", 400)
                if message["type"] != "http.request":
                    raise BridgeRequestError("BRIDGE_REQUEST_INVALID", 400)
                body.extend(message.get("body", b""))
                if len(body) > BRIDGE_MAX_REQUEST_BODY_BYTES:
                    raise BridgeRequestError("BRIDGE_REQUEST_TOO_LARGE", 413)
                if not message.get("more_body", False):
                    break
    except TimeoutError as exc:
        raise BridgeRequestError("BRIDGE_BODY_READ_TIMEOUT", 408) from exc
    if lengths and len(body) != int(lengths[0]):
        raise BridgeRequestError("BRIDGE_REQUEST_INVALID", 400)
    if request.method != "POST" and body:
        raise BridgeRequestError("BRIDGE_REQUEST_INVALID", 400)
    return bytes(body)


def _forward_headers(request: Request, route: BridgeRoute) -> dict[str, str]:
    result = {
        "Host": route.upstream_host,
        "Origin": route.upstream_origin,
        "Accept-Encoding": "identity",
    }
    for name in _MCP_HEADER_ALLOWLIST:
        values = _header_values(request.scope, name)
        if len(values) > 1:
            raise BridgeRequestError("BRIDGE_REQUEST_INVALID", 400)
        if not values or name == "accept-encoding":
            continue
        value = values[0]
        if len(value.encode("latin-1")) > 512 or _CONTROL_RE.search(value):
            raise BridgeRequestError("BRIDGE_REQUEST_INVALID", 400)
        result[name] = value

    parameter_headers: dict[str, list[str]] = {}
    parameter_header_count = 0
    for raw_name, raw_value in request.scope.get("headers", []):
        if not raw_name.lower().startswith(_MCP_PARAM_HEADER_PREFIX):
            continue
        parameter_header_count += 1
        if parameter_header_count > BRIDGE_MAX_MCP_PARAM_HEADERS:
            raise BridgeRequestError("BRIDGE_REQUEST_INVALID", 400)
        try:
            name = raw_name.decode("ascii").lower()
        except UnicodeDecodeError as exc:
            raise BridgeRequestError("BRIDGE_REQUEST_INVALID", 400) from exc
        token = name[len(_MCP_PARAM_HEADER_PREFIX) :]
        if (
            len(name.encode("ascii")) > BRIDGE_MAX_MCP_PARAM_HEADER_NAME_BYTES
            or not token
            or any(
                not character.isascii()
                or not (
                    character.isalnum()
                    or character in _MCP_PARAM_HEADER_TOKEN_CHARS
                )
                for character in token
            )
        ):
            raise BridgeRequestError("BRIDGE_REQUEST_INVALID", 400)
        parameter_headers.setdefault(name, []).append(raw_value.decode("latin-1"))

    total_parameter_header_bytes = 0
    for name, values in parameter_headers.items():
        if len(values) != 1:
            raise BridgeRequestError("BRIDGE_REQUEST_INVALID", 400)
        value = values[0]
        value_bytes = len(value.encode("latin-1"))
        total_parameter_header_bytes += len(name.encode("ascii")) + value_bytes
        if (
            value_bytes > BRIDGE_MAX_MCP_PARAM_HEADER_VALUE_BYTES
            or _CONTROL_RE.search(value)
            or total_parameter_header_bytes > BRIDGE_MAX_MCP_PARAM_HEADERS_BYTES
        ):
            raise BridgeRequestError("BRIDGE_REQUEST_INVALID", 400)
        result[name] = value
    return result


def _response_headers(response: httpx.Response) -> dict[str, str]:
    return {
        name: response.headers[name]
        for name in _RESPONSE_HEADER_ALLOWLIST
        if name in response.headers
    }


class WindowsMcpBridge:
    """Сверять идентичность исходников для фиксированных маршрутов Dev/Game до пересылки запроса."""

    def __init__(
        self,
        repository_root: Path | str,
        *,
        http_client: httpx.AsyncClient | None = None,
        upstream_observer: Callable[[BridgeRoute], BridgeUpstreamObservation]
        | None = None,
        internal_token_reader: Callable[[str | Path, str], str] = read_local_mcp_token,
    ) -> None:
        self.repository_root = Path(repository_root).absolute()
        self._http_client = http_client
        self._owns_http_client = http_client is None
        self._upstream_observer = upstream_observer or (
            lambda route: _bridge_route_status(route, self.repository_root)
        )
        self._internal_token_reader = internal_token_reader

    async def _client(self) -> httpx.AsyncClient:
        client = self._http_client
        if client is None:
            client = httpx.AsyncClient(
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(
                    connect=5.0,
                    read=BRIDGE_STREAM_IDLE_TIMEOUT_SECONDS,
                    write=30.0,
                    pool=BRIDGE_CONCURRENCY_TIMEOUT_SECONDS,
                ),
                limits=httpx.Limits(
                    max_connections=BRIDGE_MAX_CONCURRENT_REQUESTS,
                    max_keepalive_connections=BRIDGE_MAX_CONCURRENT_REQUESTS,
                ),
            )
            self._http_client = client
        return client

    async def close(self) -> None:
        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def _observe(self, route: BridgeRoute) -> BridgeUpstreamObservation:
        try:
            return await anyio.to_thread.run_sync(
                self._upstream_observer, route, abandon_on_cancel=True
            )
        except Exception as exc:  # noqa: BLE001 — наружу передаются только ограниченные сведения.
            logger.info("Не удалось проверить сервер моста: %s", type(exc).__name__)
            return BridgeUpstreamObservation("unknown", "BRIDGE_UPSTREAM_UNKNOWN")

    async def _read_identity(self, request: Request) -> BridgeSourceIdentity:
        values = _header_values(request.scope, BRIDGE_EXPECTED_IDENTITY_HEADER)
        if len(values) != 1:
            raise BridgeRequestError("BRIDGE_EXPECTED_IDENTITY_REQUIRED", 400)
        try:
            return parse_expected_identity(values[0])
        except BridgeIdentityError as exc:
            raise BridgeRequestError("BRIDGE_REQUEST_INVALID", 400) from exc

    async def identity(self, route: BridgeRoute) -> Response:
        observation = await self._observe(route)
        if observation.status != "ready" or observation.identity is None:
            status_code = 409 if observation.status == "conflict" else 503
            state = (
                observation.status
                if observation.status != "ready"
                else "unknown"
            )
            reason_code = (
                observation.reason_code
                if observation.status != "ready"
                else "BRIDGE_UPSTREAM_IDENTITY_UNKNOWN"
            )
            return JSONResponse(
                {
                    "route": route.family,
                    "server_name": route.server_name,
                    "state": state,
                    "code": reason_code,
                },
                status_code=status_code,
                headers={"cache-control": "no-store"},
            )
        return JSONResponse(
            {
                "route": route.family,
                "server_name": route.server_name,
                "state": "ready",
                "identity": observation.identity.model_dump(mode="json"),
            },
            headers={"cache-control": "no-store"},
        )

    async def mcp(self, request: Request) -> Response:
        if request.method not in {"GET", "POST", "DELETE"}:
            return _method_not_allowed(("GET", "POST", "DELETE"))
        path = request.scope.get("path")
        route = BRIDGE_ROUTES_BY_PATH.get(path)
        if route is None:  # pragma: no cover - закрытая таблица маршрутов Starlette
            return _json_error(404, "BRIDGE_ROUTE_NOT_FOUND")
        if request.scope.get("query_string"):
            return _json_error(400, "BRIDGE_REQUEST_INVALID")
        try:
            expected = await self._read_identity(request)
            forward_headers = _forward_headers(request, route)
            body = await _read_bounded_body(request)
        except BridgeRequestError as exc:
            return _json_error(exc.status_code, exc.code)

        observation = await self._observe(route)
        if observation.status != "ready" or observation.identity is None:
            status_code = 409 if observation.status == "conflict" else 503
            reason_code = (
                observation.reason_code
                if observation.status != "ready"
                else "BRIDGE_UPSTREAM_IDENTITY_UNKNOWN"
            )
            return _json_error(status_code, reason_code)
        actual = observation.identity
        mismatch = identity_mismatch_fields(expected, actual)
        if expected.server_name != route.server_name:
            mismatch = tuple(dict.fromkeys(("server_name", *mismatch)))
        if mismatch:
            return _json_error(
                409,
                "BRIDGE_SOURCE_MISMATCH",
                fields=mismatch,
                expected=expected,
                actual=actual,
            )

        try:
            internal_token = await anyio.to_thread.run_sync(
                self._internal_token_reader,
                self.repository_root,
                route.server_name,
                abandon_on_cancel=True,
            )
        except LocalHttpAuthUnknownError:
            return _json_error(503, "BRIDGE_UPSTREAM_AUTH_UNKNOWN")
        except LocalHttpAuthError, OSError:
            return _json_error(503, "BRIDGE_UPSTREAM_AUTH_UNAVAILABLE")

        forward_headers["Authorization"] = f"Bearer {internal_token}"
        forward_headers[BRIDGE_BACKEND_IDENTITY_HEADER] = serialize_identity(expected)
        try:
            client = await self._client()
            outbound = client.build_request(
                request.method,
                route.upstream_url,
                headers=forward_headers,
                content=body if request.method == "POST" else None,
                timeout=(
                    httpx.Timeout(
                        connect=5.0,
                        read=BRIDGE_STREAM_IDLE_TIMEOUT_SECONDS,
                        write=30.0,
                        pool=BRIDGE_CONCURRENCY_TIMEOUT_SECONDS,
                    )
                    if request.method == "GET"
                    else BRIDGE_REQUEST_TIMEOUT_SECONDS
                ),
            )
            # httpx переносит Set-Cookie из ответа в общую cookie-сессию. Эти
            # серверы используют bearer-аутентификацию; cookie одного маршрута не должны
            # попадать в следующий запрос или к другому серверу MCP.
            outbound.headers.pop("cookie", None)
            upstream = await client.send(
                outbound,
                stream=True,
                follow_redirects=False,
            )
        except httpx.RequestError, OSError, TimeoutError:
            return _json_error(503, "BRIDGE_UPSTREAM_UNAVAILABLE")

        if upstream.status_code == 401:
            await upstream.aclose()
            return _json_error(503, "BRIDGE_UPSTREAM_AUTH_UNAVAILABLE")
        if 300 <= upstream.status_code < 400:
            await upstream.aclose()
            return _json_error(502, "BRIDGE_UPSTREAM_REDIRECT_REJECTED")
        if upstream.status_code == 409:
            try:
                body_payload = await upstream.aread()
                payload = json.loads(body_payload.decode("utf-8"))
            except httpx.RequestError, UnicodeError, ValueError:
                payload = None
            await upstream.aclose()
            if payload == {"error": "source_identity_mismatch"}:
                latest = await self._observe(route)
                if latest.identity is None:
                    return _json_error(503, "BRIDGE_UPSTREAM_IDENTITY_UNKNOWN")
                fields = identity_mismatch_fields(expected, latest.identity)
                return _json_error(
                    409,
                    "BRIDGE_SOURCE_MISMATCH",
                    fields=fields or ("runtime_identity_changed",),
                    expected=expected,
                    actual=latest.identity,
                )
            return _json_error(409, "BRIDGE_UPSTREAM_CONFLICT")

        async def response_stream() -> AsyncIterator[bytes]:
            async for chunk in upstream.aiter_raw():
                yield chunk

        return _ClosingStreamingResponse(
            response_stream(),
            status_code=upstream.status_code,
            headers=_response_headers(upstream),
            upstream=upstream,
        )


def create_windows_mcp_bridge_app(
    repository_root: Path | str,
    *,
    http_client: httpx.AsyncClient | None = None,
    upstream_observer: Callable[[BridgeRoute], BridgeUpstreamObservation] | None = None,
    caller_token_reader: Callable[
        [str | Path], str
    ] = read_local_mcp_bridge_caller_token,
    internal_token_reader: Callable[[str | Path, str], str] = read_local_mcp_token,
) -> Starlette:
    """Создать loopback-мост с фиксированными маршрутами MCP Dev/Game."""

    root = Path(repository_root).absolute()
    bridge = WindowsMcpBridge(
        root,
        http_client=http_client,
        upstream_observer=upstream_observer,
        internal_token_reader=internal_token_reader,
    )

    async def health(request: Request) -> Response:
        if request.method != "GET":
            return _method_not_allowed(("GET",))
        return JSONResponse(
            {"ok": True, "code": "WINDOWS_MCP_BRIDGE_HEALTHY"},
            headers={"cache-control": "no-store"},
        )

    async def ready(request: Request) -> Response:
        if request.method != "GET":
            return _method_not_allowed(("GET",))
        return JSONResponse(
            {
                "ok": True,
                "code": "LOCAL_MCP_READY",
                "server_name": BRIDGE_NAME,
                "transport": "windows_mcp_bridge",
            },
            headers={"cache-control": "no-store"},
        )

    routes = [
        Route("/health", endpoint=health, methods=["GET"]),
        Route("/ready", endpoint=ready, methods=["GET"]),
    ]
    for route in BRIDGE_ROUTES.values():
        routes.append(
            Route(route.path, endpoint=bridge.mcp, methods=["GET", "POST", "DELETE"])
        )

        async def identity_endpoint(
            request: Request, item: BridgeRoute = route
        ) -> Response:
            if request.method != "GET":
                return _method_not_allowed(("GET",))
            return await bridge.identity(item)

        routes.append(
            Route(
                route.identity_path,
                endpoint=identity_endpoint,
                methods=["GET"],
            )
        )

    @asynccontextmanager
    async def lifespan(_: Starlette):
        try:
            yield
        finally:
            await bridge.close()

    app = Starlette(debug=False, routes=routes, lifespan=lifespan)
    app.add_middleware(
        BridgeCallerAuthMiddleware,
        repository_root=root,
        token_reader=caller_token_reader,
    )
    app.add_middleware(BridgeHostOriginMiddleware)
    app.add_middleware(BridgeTimeoutMiddleware)
    app.add_middleware(BridgeConcurrencyMiddleware)
    app.add_middleware(BridgeFailSafeMiddleware)
    app.state.windows_mcp_bridge = bridge
    return app


def _default_repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=_default_repository_root())
    args = parser.parse_args(argv)
    if not _supports_windows_mcp_bridge():
        logger.error("Мост Windows MCP можно запустить только в Windows")
        return 2
    try:
        # До привязки проверить учётные данные; статус не раскрывает их значения.
        read_local_mcp_bridge_caller_token(args.root)
        import uvicorn

        uvicorn.run(
            create_windows_mcp_bridge_app(args.root),
            host=BRIDGE_BIND_HOST,
            port=BRIDGE_PORT,
            log_config=None,
            access_log=False,
            log_level="warning",
        )
    except LocalHttpAuthUnknownError:
        logger.error("Учётные данные вызывающего клиента локального моста неизвестны")
        return 2
    except LocalHttpAuthError:
        logger.error("Учётные данные вызывающего клиента локального моста недоступны")
        return 2
    except OSError as exc:
        logger.error("Мост Windows MCP не запущен: %s", type(exc).__name__)
        return 2
    return 0


__all__ = (
    "BridgeUpstreamObservation",
    "WindowsMcpBridge",
    "create_windows_mcp_bridge_app",
    "main",
)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
