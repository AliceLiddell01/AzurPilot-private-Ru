"""Канонический ограниченный контракт моста Windows для MCP Dev/Game."""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from module.mcp_shared.local_http_constants import LOCAL_HTTP_ENDPOINTS

BRIDGE_NAME = "windows-mcp-bridge"
BRIDGE_BIND_HOST = "127.0.0.1"
BRIDGE_PORT = 8780
BRIDGE_CALLER_TOKEN_ENV_VAR = "AZURPILOT_MCP_BRIDGE_CALLER_TOKEN"
BRIDGE_IDENTITY_PROTOCOL = "azurpilot-mcp-source-identity/v1"
BRIDGE_EXPECTED_IDENTITY_HEADER = "x-azurpilot-expected-source-identity"
BRIDGE_BACKEND_IDENTITY_HEADER = "x-azurpilot-backend-source-identity"
BRIDGE_MAX_IDENTITY_HEADER_BYTES = 2048
BRIDGE_MAX_MCP_PARAM_HEADERS = 32
BRIDGE_MAX_MCP_PARAM_HEADER_NAME_BYTES = 128
BRIDGE_MAX_MCP_PARAM_HEADER_VALUE_BYTES = 512
BRIDGE_MAX_MCP_PARAM_HEADERS_BYTES = 4096
BRIDGE_MAX_REQUEST_BODY_BYTES = 1024 * 1024
BRIDGE_MAX_CONCURRENT_REQUESTS = 8
BRIDGE_CONCURRENCY_TIMEOUT_SECONDS = 2.0
BRIDGE_BODY_READ_TIMEOUT_SECONDS = 10.0
BRIDGE_REQUEST_TIMEOUT_SECONDS = 180.0
BRIDGE_STREAM_IDLE_TIMEOUT_SECONDS = 180.0


class BridgeIdentityError(ValueError):
    """Ожидаемая идентичность отсутствует, повреждена или превышает допустимый размер."""


class BridgeSourceIdentity(BaseModel):
    """Закрытое описание идентичности одной серверной части MCP проекта."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    identity_protocol: Literal["azurpilot-mcp-source-identity/v1"]
    server_name: Literal["azurpilot-dev", "azurpilot-game"]
    server_version: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,127}$",
        min_length=1,
        max_length=128,
    )
    source_revision: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    source_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    contract_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    capability_catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class BridgeRoute:
    """Фиксированный маршрут без возможности выбрать host или port из запроса."""

    family: Literal["dev", "game"]
    path: str
    identity_path: str
    server_name: Literal["azurpilot-dev", "azurpilot-game"]
    upstream_url: str

    @property
    def upstream_host(self) -> str:
        parsed = urlsplit(self.upstream_url)
        if parsed.hostname != "127.0.0.1" or parsed.port is None:
            raise RuntimeError("Канонический сервер MCP больше не использует loopback")
        return f"127.0.0.1:{parsed.port}"

    @property
    def upstream_origin(self) -> str:
        return f"http://{self.upstream_host}"

    @property
    def readiness_url(self) -> str:
        parsed = urlsplit(self.upstream_url)
        return f"{parsed.scheme}://{parsed.netloc}/ready"

    @property
    def bridge_url(self) -> str:
        return f"{bridge_endpoint()}{self.path}"


def bridge_endpoint() -> str:
    """Вернуть каноническую базовую точку входа моста."""

    return f"http://{BRIDGE_BIND_HOST}:{BRIDGE_PORT}"


BRIDGE_ROUTES = MappingProxyType(
    {
        "dev": BridgeRoute(
            family="dev",
            path="/dev/mcp",
            identity_path="/identity/dev",
            server_name="azurpilot-dev",
            upstream_url=LOCAL_HTTP_ENDPOINTS["azurpilot-dev"],
        ),
        "game": BridgeRoute(
            family="game",
            path="/game/mcp",
            identity_path="/identity/game",
            server_name="azurpilot-game",
            upstream_url=LOCAL_HTTP_ENDPOINTS["azurpilot-game"],
        ),
    }
)
BRIDGE_ROUTES_BY_PATH = MappingProxyType(
    {route.path: route for route in BRIDGE_ROUTES.values()}
)
BRIDGE_ROUTES_BY_IDENTITY_PATH = MappingProxyType(
    {route.identity_path: route for route in BRIDGE_ROUTES.values()}
)


def _reject_duplicate_object_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BridgeIdentityError("Идентичность содержит повторяющееся поле")
        result[key] = value
    return result


def parse_expected_identity(value: str | bytes | None) -> BridgeSourceIdentity:
    """Разобрать один ограниченный JSON-заголовок в закрытую сетевую идентичность."""

    if isinstance(value, bytes):
        if len(value) > BRIDGE_MAX_IDENTITY_HEADER_BYTES:
            raise BridgeIdentityError("Идентичность превышает допустимый размер")
        try:
            raw = value.decode("ascii")
        except UnicodeDecodeError as exc:
            raise BridgeIdentityError("Идентичность должна содержать только символы ASCII") from exc
    elif isinstance(value, str):
        try:
            encoded = value.encode("ascii")
        except UnicodeEncodeError as exc:
            raise BridgeIdentityError("Идентичность должна содержать только символы ASCII") from exc
        if len(encoded) > BRIDGE_MAX_IDENTITY_HEADER_BYTES:
            raise BridgeIdentityError("Идентичность превышает допустимый размер")
        raw = value
    else:
        raise BridgeIdentityError("Идентичность отсутствует")
    if not raw:
        raise BridgeIdentityError("Идентичность отсутствует")

    def reject_constant(_value: str) -> object:
        raise BridgeIdentityError("Идентичность содержит недопустимое значение")

    try:
        decoded = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_object_keys,
            parse_constant=reject_constant,
        )
        return BridgeSourceIdentity.model_validate(decoded)
    except BridgeIdentityError:
        raise
    except (json.JSONDecodeError, TypeError, ValidationError) as exc:
        raise BridgeIdentityError("Идентичность не соответствует сетевому контракту") from exc


def identity_from_ready_payload(payload: object) -> BridgeSourceIdentity:
    """Построить идентичность только из известных полей готовности среды выполнения."""

    if not isinstance(payload, dict):
        raise BridgeIdentityError("Идентичность среды выполнения отсутствует")
    expected = {
        "identity_protocol": BRIDGE_IDENTITY_PROTOCOL,
        **{
            name: payload.get(name)
            for name in (
                "server_name",
                "server_version",
                "source_revision",
                "source_set_digest",
                "contract_revision",
                "tool_catalog_sha256",
                "capability_catalog_sha256",
            )
        },
    }
    try:
        return BridgeSourceIdentity.model_validate(expected)
    except (TypeError, ValidationError) as exc:
        raise BridgeIdentityError(
            "Идентичность среды выполнения отсутствует или имеет неверный формат"
        ) from exc


def serialize_identity(identity: BridgeSourceIdentity) -> str:
    """Сериализовать идентичность в канонический ограниченный JSON одного заголовка."""

    value = json.dumps(
        identity.model_dump(mode="json"),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(value.encode("ascii")) > BRIDGE_MAX_IDENTITY_HEADER_BYTES:
        raise BridgeIdentityError("Идентичность превышает допустимый размер")
    return value


def identity_mismatch_fields(
    expected: BridgeSourceIdentity, actual: BridgeSourceIdentity
) -> tuple[str, ...]:
    """Вернуть только имена полей контракта, значения которых различаются."""

    return tuple(
        field
        for field in (
            "server_name",
            "server_version",
            "source_revision",
            "source_set_digest",
            "contract_revision",
            "tool_catalog_sha256",
            "capability_catalog_sha256",
        )
        if getattr(expected, field) != getattr(actual, field)
    )


__all__ = (
    "BRIDGE_BACKEND_IDENTITY_HEADER",
    "BRIDGE_BIND_HOST",
    "BRIDGE_BODY_READ_TIMEOUT_SECONDS",
    "BRIDGE_CALLER_TOKEN_ENV_VAR",
    "BRIDGE_CONCURRENCY_TIMEOUT_SECONDS",
    "BRIDGE_EXPECTED_IDENTITY_HEADER",
    "BRIDGE_IDENTITY_PROTOCOL",
    "BRIDGE_MAX_CONCURRENT_REQUESTS",
    "BRIDGE_MAX_IDENTITY_HEADER_BYTES",
    "BRIDGE_MAX_MCP_PARAM_HEADERS",
    "BRIDGE_MAX_MCP_PARAM_HEADER_NAME_BYTES",
    "BRIDGE_MAX_MCP_PARAM_HEADER_VALUE_BYTES",
    "BRIDGE_MAX_MCP_PARAM_HEADERS_BYTES",
    "BRIDGE_MAX_REQUEST_BODY_BYTES",
    "BRIDGE_NAME",
    "BRIDGE_PORT",
    "BRIDGE_REQUEST_TIMEOUT_SECONDS",
    "BRIDGE_ROUTES",
    "BRIDGE_ROUTES_BY_IDENTITY_PATH",
    "BRIDGE_ROUTES_BY_PATH",
    "BRIDGE_STREAM_IDLE_TIMEOUT_SECONDS",
    "BridgeIdentityError",
    "BridgeRoute",
    "BridgeSourceIdentity",
    "bridge_endpoint",
    "identity_from_ready_payload",
    "identity_mismatch_fields",
    "parse_expected_identity",
    "serialize_identity",
)
