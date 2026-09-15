"""Точка входа локального Streamable HTTP для Dev MCP."""

from __future__ import annotations

import logging
import os
import re
import sys
from typing import Any

import uvicorn

from module.dev_mcp.adapter import DevMcpAdapter
from module.dev_mcp.contract import (
    DEV_MCP_REQUIRED_SCOPE,
    DEV_MCP_SERVER_NAME,
    contract_payload,
)
from module.dev_mcp.server import create_server
from module.mcp_shared.local_http import (
    LocalHttpConfig,
    LocalHttpConfigError,
)
from module.mcp_shared.local_http import (
    create_local_http_app as _create_local_http_app,
)

logger = logging.getLogger(__name__)

DEV_MCP_LOCAL_HTTP_PORT = 8775
DEV_MCP_LOCAL_HTTP_TOKEN_ENV_VAR = "AZURPILOT_DEV_LOCAL_MCP_TOKEN"
DEV_MCP_SOURCE_SET_DIGEST_ENV_VAR = "AZURPILOT_DEV_MCP_SOURCE_SET_DIGEST"
_SOURCE_SET_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def default_config() -> LocalHttpConfig:
    """Загрузить bounded local HTTP config из user environment."""

    return LocalHttpConfig.from_env(
        server_name=DEV_MCP_SERVER_NAME,
        port=DEV_MCP_LOCAL_HTTP_PORT,
        required_scope=DEV_MCP_REQUIRED_SCOPE,
        token_env_var=DEV_MCP_LOCAL_HTTP_TOKEN_ENV_VAR,
    )


def create_local_http_app(
    adapter: Any | None = None,
    *,
    config: LocalHttpConfig | None = None,
) -> Any:
    """Создать stateless authenticated Dev MCP loopback app."""

    local_config = config or default_config()
    bound_adapter = adapter if adapter is not None else DevMcpAdapter()
    metadata = contract_payload()
    digest = os.environ.get(DEV_MCP_SOURCE_SET_DIGEST_ENV_VAR, "").strip().lower()
    if _SOURCE_SET_DIGEST_RE.fullmatch(digest):
        metadata["source_set_digest"] = digest
    return _create_local_http_app(
        create_server,
        bound_adapter,
        config=local_config,
        identity_metadata=metadata,
    )


def run_local_http_server(
    adapter: Any | None = None,
    config: LocalHttpConfig | None = None,
) -> None:
    """Запустить Dev MCP только на loopback local HTTP endpoint."""

    local_config = config or default_config()
    app = create_local_http_app(adapter, config=local_config)
    uvicorn.run(
        app,
        host=local_config.bind_host,
        port=local_config.port,
        proxy_headers=False,
        access_log=False,
        server_header=False,
        date_header=False,
        log_level="warning",
        timeout_keep_alive=5,
    )


def main() -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        run_local_http_server()
    except LocalHttpConfigError as exc:
        logger.error("Конфигурация Dev MCP local HTTP отклонена: %s", exc)
        raise SystemExit(2) from None


__all__ = (
    "DEV_MCP_LOCAL_HTTP_PORT",
    "DEV_MCP_LOCAL_HTTP_TOKEN_ENV_VAR",
    "DEV_MCP_SOURCE_SET_DIGEST_ENV_VAR",
    "LocalHttpConfig",
    "LocalHttpConfigError",
    "create_local_http_app",
    "default_config",
    "main",
    "run_local_http_server",
)


if __name__ == "__main__":  # pragma: no cover
    main()
