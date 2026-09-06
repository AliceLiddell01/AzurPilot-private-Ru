"""Декларативный registry ключей локального `.env` AzurPilot."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

EnvironmentScope = Literal["postgres", "wsl", "infrastructure"]


@dataclass(frozen=True, slots=True)
class LocalEnvironmentKey:
    """Описание одного ключа общего локального environment-файла."""

    name: str
    scope: EnvironmentScope
    secret: bool = False


_CONNECTION_FIELDS = (
    "HOST",
    "PORT",
    "DATABASE",
    "USER",
    "PASSWORD",
    "SSLMODE",
    "RUNTIME_TIMEZONE",
    "PGPASSFILE",
)


def _postgres_keys(prefix: str) -> tuple[LocalEnvironmentKey, ...]:
    return tuple(
        LocalEnvironmentKey(
            name=f"{prefix}{field_name}",
            scope="postgres",
            secret=field_name == "PASSWORD",
        )
        for field_name in _CONNECTION_FIELDS
    )


LOCAL_ENVIRONMENT_REGISTRY = (
    *_postgres_keys("AZURPILOT_POSTGRES_"),
    *_postgres_keys("AZURPILOT_POSTGRES_MIGRATOR_"),
    LocalEnvironmentKey("AZURPILOT_WSL_DISTRO", "wsl"),
    LocalEnvironmentKey("AZURPILOT_WSL_PGPASSFILE", "wsl"),
    LocalEnvironmentKey(
        "AZURPILOT_POSTGRES_DOCKER_BOOTSTRAP_PASSWORD",
        "infrastructure",
        secret=True,
    ),
    LocalEnvironmentKey("AZURPILOT_OBSERVABILITY_GRAFANA_ADMIN_USER", "infrastructure"),
    LocalEnvironmentKey(
        "AZURPILOT_OBSERVABILITY_GRAFANA_ADMIN_PASSWORD",
        "infrastructure",
        secret=True,
    ),
    LocalEnvironmentKey(
        "AZURPILOT_OBSERVABILITY_PGADMIN_ADMIN_EMAIL",
        "infrastructure",
    ),
    LocalEnvironmentKey(
        "AZURPILOT_OBSERVABILITY_PGADMIN_ADMIN_PASSWORD",
        "infrastructure",
        secret=True,
    ),
    LocalEnvironmentKey(
        "AZURPILOT_OBSERVABILITY_PGADMIN_PGPASS",
        "infrastructure",
        secret=True,
    ),
    LocalEnvironmentKey("AZURPILOT_OBSERVABILITY_PGADMIN_PORT", "infrastructure"),
    LocalEnvironmentKey("AZURPILOT_CADDY_HOST", "infrastructure"),
    LocalEnvironmentKey("AZURPILOT_GAME_MCP_PUBLIC_HOST", "infrastructure"),
)

_REGISTRY_BY_NAME = {entry.name: entry for entry in LOCAL_ENVIRONMENT_REGISTRY}
if len(_REGISTRY_BY_NAME) != len(LOCAL_ENVIRONMENT_REGISTRY):
    raise RuntimeError("Registry локального environment содержит дублирующийся ключ.")

LOCAL_ENVIRONMENT_KEYS = frozenset(_REGISTRY_BY_NAME)
POSTGRES_ENVIRONMENT_KEYS = frozenset(
    entry.name for entry in LOCAL_ENVIRONMENT_REGISTRY if entry.scope == "postgres"
)
WSL_ENVIRONMENT_KEYS = frozenset(
    entry.name for entry in LOCAL_ENVIRONMENT_REGISTRY if entry.scope == "wsl"
)
INFRASTRUCTURE_ENVIRONMENT_KEYS = frozenset(
    entry.name
    for entry in LOCAL_ENVIRONMENT_REGISTRY
    if entry.scope == "infrastructure"
)
SECRET_ENVIRONMENT_KEYS = frozenset(
    entry.name for entry in LOCAL_ENVIRONMENT_REGISTRY if entry.secret
)


def get_local_environment_key(name: str) -> LocalEnvironmentKey | None:
    """Вернуть точное описание ключа или ``None`` для неизвестного имени."""

    return _REGISTRY_BY_NAME.get(name)
