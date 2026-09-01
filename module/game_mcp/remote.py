"""Публичный authenticated Streamable HTTP entrypoint для Game MCP."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from collections.abc import Callable
from typing import Any

import uvicorn

from module.game_mcp.adapter import GameMcpAdapter
from module.game_mcp.server import (
    GAME_MCP_REQUIRED_SCOPE,
    create_server,
)
from module.mcp_shared.remote import (
    DEFAULT_ALLOWED_ORIGINS,
    DEFAULT_BODY_READ_TIMEOUT_SECONDS,
    DEFAULT_CONCURRENCY_ACQUIRE_TIMEOUT_SECONDS,
    DEFAULT_MAX_CONCURRENT_REQUESTS,
    DEFAULT_MAX_REQUEST_BODY_BYTES,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_VERIFICATION_TIMEOUT_SECONDS,
    MCP_PATH,
    ConcurrencyLimitMiddleware,
    FailSafeMiddleware,
    RemoteConfigError,
    RequestBodyLimitMiddleware,
    RequestTimeoutMiddleware,
    StrictHostOriginMiddleware,
)
from module.mcp_shared.remote import (
    OAuthBearerMiddleware as _OAuthBearerMiddleware,
)
from module.mcp_shared.remote import (
    OIDCTokenVerifier as _OIDCTokenVerifier,
)
from module.mcp_shared.remote import (
    RemoteConfig as _RemoteConfig,
)
from module.mcp_shared.remote import (
    create_remote_app as _create_remote_app,
)

GAME_MCP_PORT = 8766


class RemoteConfig(_RemoteConfig):
    """Конфигурация Game MCP с независимым Game environment prefix."""

    ENV_PREFIX = "AZURPILOT_GAME_MCP"

    @classmethod
    def from_env(cls, prefix: str = ENV_PREFIX) -> RemoteConfig:
        return super().from_env(prefix)


class OIDCTokenVerifier(_OIDCTokenVerifier):
    """Game-обёртка над нейтральной проверкой resource token."""

    def __init__(
        self,
        config: RemoteConfig,
        *,
        jwk_client: Any | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(
            config,
            required_scope=GAME_MCP_REQUIRED_SCOPE,
            jwk_client=jwk_client,
            clock=clock,
        )


class OAuthBearerMiddleware(_OAuthBearerMiddleware):
    """Game wrapper с фиксированным отдельным Game scope."""

    def __init__(self, app: Any, config: RemoteConfig, verifier: Any) -> None:
        super().__init__(
            app,
            config,
            verifier,
            required_scope=GAME_MCP_REQUIRED_SCOPE,
        )


def _create_remote_server(adapter: Any, *, abandon_on_cancel: bool) -> Any:
    """Создать HTTP MCP server без перехвата stdout процесса."""
    return create_server(
        adapter,
        abandon_on_cancel=abandon_on_cancel,
        redirect_legacy_stdout=False,
    )


def create_remote_app(
    adapter: Any | None = None,
    *,
    config: RemoteConfig | None = None,
    token_verifier: Any | None = None,
) -> Any:
    """Создать stateless authenticated Game MCP ASGI app."""

    remote_config = config or RemoteConfig.from_env()
    verifier = token_verifier or OIDCTokenVerifier(remote_config)
    bound_adapter = adapter if adapter is not None else GameMcpAdapter()
    return _create_remote_app(
        _create_remote_server,
        bound_adapter,
        config=remote_config,
        token_verifier=verifier,
        required_scope=GAME_MCP_REQUIRED_SCOPE,
    )


def run_remote_server(
    adapter: Any | None = None, config: RemoteConfig | None = None
) -> None:
    """Запустить Game MCP на loopback для reverse proxy."""

    remote_config = config or RemoteConfig.from_env()
    app = create_remote_app(adapter, config=remote_config)
    uvicorn.run(
        app,
        host=remote_config.bind_host,
        port=GAME_MCP_PORT,
        proxy_headers=False,
        access_log=False,
        server_header=False,
        date_header=False,
        log_level="warning",
        timeout_keep_alive=5,
    )


def doctor() -> int:
    try:
        config = RemoteConfig.from_env()
    except RemoteConfigError as exc:
        print(
            json.dumps(
                {"ok": False, "code": "REMOTE_CONFIG_INVALID", "message": str(exc)},
                ensure_ascii=False,
            )
        )
        return 1
    caddy_available = shutil.which("caddy") is not None
    print(
        json.dumps(
            {
                "ok": caddy_available,
                "code": "REMOTE_CONFIG_READY"
                if caddy_available
                else "CADDY_NOT_AVAILABLE",
                "bind_host_loopback": config.bind_host == "127.0.0.1",
                "public_https_path": config.public_url.endswith(MCP_PATH),
                "caddy_available": caddy_available,
            },
            ensure_ascii=False,
        )
    )
    return 0 if caddy_available else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Публичная точка входа AzurPilot Game MCP"
    )
    parser.add_argument(
        "command", nargs="?", choices=("serve", "doctor"), default="serve"
    )
    args = parser.parse_args()
    if args.command == "doctor":
        raise SystemExit(doctor())
    try:
        run_remote_server()
    except RemoteConfigError as exc:
        print(
            json.dumps(
                {"ok": False, "code": "REMOTE_CONFIG_INVALID", "message": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        raise SystemExit(2) from None


__all__ = (
    "DEFAULT_ALLOWED_ORIGINS",
    "DEFAULT_BODY_READ_TIMEOUT_SECONDS",
    "DEFAULT_CONCURRENCY_ACQUIRE_TIMEOUT_SECONDS",
    "DEFAULT_MAX_CONCURRENT_REQUESTS",
    "DEFAULT_MAX_REQUEST_BODY_BYTES",
    "DEFAULT_REQUEST_TIMEOUT_SECONDS",
    "DEFAULT_VERIFICATION_TIMEOUT_SECONDS",
    "GAME_MCP_PORT",
    "GAME_MCP_REQUIRED_SCOPE",
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
    "doctor",
    "main",
    "run_remote_server",
)


if __name__ == "__main__":  # pragma: no cover
    main()
