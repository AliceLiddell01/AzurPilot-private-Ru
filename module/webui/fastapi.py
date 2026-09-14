"""Совместимый адаптер PyWebIO для AzurPilot WebUI."""

import asyncio
import logging
import os
from collections.abc import Mapping
from contextlib import asynccontextmanager
from inspect import isawaitable
from typing import Any, cast

import pywebio.platform.fastapi as pywebio_fastapi
import uvicorn
from pywebio.platform.fastapi import (
    STATIC_PATH,
    Session,
    cdn_validation,
    get_free_port,
    open_webbrowser_on_server_started,
    start_remote_access_service,
    webio_routes,
)
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocketDisconnect

ROBOTS_TXT = """\
User-agent: *
Disallow: /
"""

logger = logging.getLogger(__name__)

STATIC_ASSET_CACHE_CONTROL = "no-cache"
NO_CACHE_CONTROL = "no-cache"
HTTP_GZIP_MINIMUM_SIZE = 1024
HTTP_GZIP_COMPRESS_LEVEL = 5
DISABLED_API_ROUTE_PATHS = {
    "/ws/live_screenshot",
    "/ws/live_control",
}


async def _run_lifecycle_handlers(handlers) -> None:
    """Выполнить legacy startup/shutdown handlers с поддержкой async callable."""
    for handler in handlers:
        result = handler()
        if isawaitable(result):
            await result


def _legacy_lifespan(on_startup, on_shutdown):
    """Преобразовать lifecycle API PyWebIO в lifespan API Starlette 1.6+."""

    @asynccontextmanager
    async def lifespan(_app):
        await _run_lifecycle_handlers(on_startup)
        try:
            yield
        finally:
            await _run_lifecycle_handlers(on_shutdown)

    return lifespan


class HeaderMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        path = request.url.path
        is_static_asset = path.startswith(("/static/assets/", "/pywebio_static/"))
        is_cacheable_response = (
            200 <= response.status_code < 300 or response.status_code == 304
        )
        if request.method in {"GET", "HEAD"} and is_static_asset and is_cacheable_response:
            # У части статических ресурсов нет хеша содержимого, поэтому их нужно
            # перепроверять при каждом использовании.
            response.headers["Cache-Control"] = STATIC_ASSET_CACHE_CONTROL
        else:
            response.headers["Cache-Control"] = NO_CACHE_CONTROL
        response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
        return response


async def robots_txt(request):
    return PlainTextResponse(
        ROBOTS_TXT,
        media_type="text/plain",
        headers={"X-Robots-Tag": "noindex, nofollow, noarchive"},
    )


class SafeWebSocketConnection(pywebio_fastapi.WebSocketConnection):
    """
    Starlette/websockets не разрешают параллельную отправку через одно соединение.

    Стандартная реализация PyWebIO создаёт отдельную task для каждого сообщения;
    при нескольких выводах за одно событие нижележащий drain может завершиться
    с AssertionError и вывести "Task exception was never retrieved".
    """

    def __init__(self, websocket, ioloop):
        super().__init__(websocket, ioloop)
        self._send_lock = asyncio.Lock()

    async def _safe_send_json(self, message):
        async with self._send_lock:
            if self.closed():
                return
            try:
                await self.ws.send_json(message)
            except TypeError:
                logger.exception("Не удалось сериализовать сообщение PyWebIO; содержимое: %s", message)
            except AssertionError, RuntimeError, WebSocketDisconnect:
                logger.debug("WebSocket уже отключён; отправка сообщения PyWebIO пропущена")
            except Exception as e:
                logger.debug("Не удалось отправить сообщение PyWebIO через WebSocket: %s", e)

    async def _safe_close(self):
        async with self._send_lock:
            if self.closed():
                return
            try:
                await self.ws.close()
            except AssertionError, RuntimeError, WebSocketDisconnect:
                logger.debug("WebSocket уже отключён; повторное закрытие соединения PyWebIO пропущено")
            except Exception as e:
                logger.debug("Не удалось закрыть соединение PyWebIO WebSocket: %s", e)

    def write_message(self, message: dict):
        self.ioloop.create_task(self._safe_send_json(message))

    def close(self):
        self.ioloop.create_task(self._safe_close())


def patch_pywebio_websocket_connection():
    pywebio_fastapi.WebSocketConnection = SafeWebSocketConnection


def asgi_app(
    applications,
    cdn: str | bool = False,
    static_dir=None,
    debug: bool = False,
    allowed_origins=None,
    check_origin=None,
    static_mounts: Mapping[str, str] | None = None,
    **starlette_settings,
):
    on_startup = tuple(starlette_settings.pop("on_startup", ()) or ())
    on_shutdown = tuple(starlette_settings.pop("on_shutdown", ()) or ())
    if on_startup or on_shutdown:
        if starlette_settings.get("lifespan") is not None:
            raise TypeError(
                "Нельзя одновременно передавать on_startup/on_shutdown и lifespan."
            )
        starlette_settings["lifespan"] = _legacy_lifespan(on_startup, on_shutdown)

    debug = bool(os.environ.get("PYWEBIO_DEBUG", debug))
    Session.debug = debug
    validated_cdn: str | bool = cdn_validation(cdn, "warn")
    if validated_cdn is False:
        validated_cdn = "pywebio_static"
    patch_pywebio_websocket_connection()
    routes = webio_routes(
        applications,
        # PyWebIO принимает строковый CDN, хотя его runtime-аннотация оставляет только bool.
        cdn=cast(Any, validated_cdn),
        allowed_origins=allowed_origins,
        check_origin=check_origin,
    )
    routes.insert(0, Route("/robots.txt", robots_txt, methods=["GET", "HEAD"]))
    if static_mounts:
        for mount_path, directory in static_mounts.items():
            routes.append(Mount(mount_path, app=StaticFiles(directory=directory)))
    if static_dir:
        routes.append(
            Mount("/static", app=StaticFiles(directory=static_dir), name="static")
        )
    routes.append(
        Mount(
            "/pywebio_static",
            app=StaticFiles(directory=STATIC_PATH),
            name="pywebio_static",
        )
    )

    try:
        from module.webui.api import api_routes

        routes.extend(
            route
            for route in api_routes
            if getattr(route, "path", None) not in DISABLED_API_ROUTE_PATHS
        )
    except Exception as e:
        import logging

        logging.getLogger(__name__).error(f"Не удалось загрузить маршруты API: {e}")

    middleware = [
        # Обрабатываем только HTTP-ответы; WebSocket и SSE проходят мимо middleware.
        Middleware(
            GZipMiddleware,
            minimum_size=HTTP_GZIP_MINIMUM_SIZE,
            compresslevel=HTTP_GZIP_COMPRESS_LEVEL,
        ),
        Middleware(HeaderMiddleware),
    ]
    return Starlette(
        routes=routes, middleware=middleware, debug=debug, **starlette_settings
    )


def start_server(
    applications,
    port=0,
    host="",
    cdn: str | bool = False,
    static_dir=None,
    remote_access=False,
    debug=False,
    allowed_origins=None,
    check_origin=None,
    auto_open_webbrowser=False,
    static_mounts: Mapping[str, str] | None = None,
    **uvicorn_settings,
):

    app = asgi_app(
        applications,
        cdn=cdn,
        static_dir=static_dir,
        static_mounts=static_mounts,
        debug=debug,
        allowed_origins=allowed_origins,
        check_origin=check_origin,
    )

    if auto_open_webbrowser:
        asyncio.get_event_loop().create_task(
            open_webbrowser_on_server_started("localhost", port)
        )

    if not host:
        host = "0.0.0.0"

    if port == 0:
        port = get_free_port()

    if remote_access:
        start_remote_access_service(local_port=port)

    uvicorn.run(app, host=host, port=port, **uvicorn_settings)
