"""Канонический владелец ожидаемой source identity моста Windows MCP.

Сборка ожидаемой идентичности выделена из `McpService._bridge_accept`, чтобы
приёмка Windows-моста и Linux-клиент DeepSeek Harness использовали один и тот
же алгоритм. Второго вычисления ревизии, дайджестов наборов исходников и
каталогов инструментов здесь нет: значения берутся из канонического комплекта
MCP, а сериализация — из wire-контракта моста.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from module.mcp_shared.versioning import McpBundle, VersioningError, load_mcp_bundle
from module.mcp_shared.windows_mcp_bridge_contract import (
    BRIDGE_EXPECTED_IDENTITY_HEADER,
    BRIDGE_IDENTITY_PROTOCOL,
    BRIDGE_ROUTES,
    BridgeSourceIdentity,
    identity_mismatch_fields,
    serialize_identity,
)

from .contracts import ResultCode
from .errors import ToolingError
from .git import GitClient

__all__ = (
    "BRIDGE_MCP_SERVER_NAMES",
    "expected_bridge_headers",
    "expected_bridge_identities",
    "expected_bridge_identity",
    "identity_drift_fields",
    "identity_header_value",
    "load_bridge_bundle",
    "source_snapshot",
)

BRIDGE_MCP_SERVER_NAMES: tuple[str, ...] = tuple(
    route.server_name for route in BRIDGE_ROUTES.values()
)


def source_snapshot(root: Path | str) -> tuple[str, str]:
    """Вернуть commit SHA checkout и состояние его рабочей копии.

    Второе значение — ``clean`` или ``modified``; оба состояния вычисляются
    единственным владельцем Git-команд проекта, без собственного разбора вывода.
    """

    client = GitClient(Path(root))
    revision = client.head()
    porcelain = client.status_porcelain()
    return revision, ("clean" if not porcelain.strip() else "modified")


def load_bridge_bundle(root: Path | str) -> McpBundle:
    """Загрузить канонический комплект MCP с единой диагностикой ошибок."""

    try:
        return load_mcp_bundle(Path(root))
    except VersioningError as exc:
        raise ToolingError(
            ResultCode.MCP_SOURCE_BUNDLE_INVALID,
            "Канонический комплект MCP имеет неверный формат.",
        ) from exc


def expected_bridge_identity(
    bundle: McpBundle,
    server_name: str,
    revision: str,
) -> BridgeSourceIdentity:
    """Собрать ожидаемую идентичность одного семейства моста."""

    try:
        server = bundle.servers[server_name]
    except KeyError as exc:  # pragma: no cover - закрытый каталог серверов моста
        raise ToolingError(
            ResultCode.MCP_SOURCE_BUNDLE_INVALID,
            "Канонический комплект MCP не содержит сервер моста.",
        ) from exc
    return BridgeSourceIdentity(
        identity_protocol=BRIDGE_IDENTITY_PROTOCOL,
        server_name=server_name,
        server_version=str(server.version),
        source_revision=revision,
        source_set_digest=server.source_set_digest,
        contract_revision=server.contract_revision,
        tool_catalog_sha256=server.tool_catalog_sha256,
        capability_catalog_sha256=server.capability_catalog_sha256,
    )


def expected_bridge_identities(
    bundle: McpBundle,
    revision: str,
) -> Mapping[str, BridgeSourceIdentity]:
    """Собрать ожидаемые идентичности обоих маршрутов моста."""

    return {
        server_name: expected_bridge_identity(bundle, server_name, revision)
        for server_name in BRIDGE_MCP_SERVER_NAMES
    }


def identity_header_value(identity: BridgeSourceIdentity) -> str:
    """Сериализовать идентичность каноническим контрактом моста."""

    return serialize_identity(identity)


def expected_bridge_headers(
    caller_token: str,
    identity: BridgeSourceIdentity,
) -> dict[str, str]:
    """Собрать заголовки одного маршрута моста, не раскрывая токен наружу."""

    return {
        "Authorization": f"Bearer {caller_token}",
        BRIDGE_EXPECTED_IDENTITY_HEADER: identity_header_value(identity),
    }


def identity_drift_fields(
    recorded: BridgeSourceIdentity,
    current: BridgeSourceIdentity,
) -> tuple[str, ...]:
    """Вернуть поля идентичности, различающиеся между генерацией и checkout."""

    return identity_mismatch_fields(recorded, current)
