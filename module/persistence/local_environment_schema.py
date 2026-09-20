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
    LocalEnvironmentKey("AZURPILOT_REDIS_HOST", "infrastructure"),
    LocalEnvironmentKey("AZURPILOT_REDIS_PORT", "infrastructure"),
    LocalEnvironmentKey("AZURPILOT_REDIS_USERNAME", "infrastructure"),
    LocalEnvironmentKey("AZURPILOT_REDIS_PASSWORD", "infrastructure", secret=True),
    LocalEnvironmentKey(
        "AZURPILOT_REDIS_ADMIN_PASSWORD", "infrastructure", secret=True
    ),
    LocalEnvironmentKey(
        "AZURPILOT_REDISINSIGHT_ENCRYPTION_KEY",
        "infrastructure",
        secret=True,
    ),
    LocalEnvironmentKey("AZURPILOT_REDISINSIGHT_PORT", "infrastructure"),
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
    LocalEnvironmentKey("AZURPILOT_NOTIFICATION_AGENT_BACKEND", "infrastructure"),
    LocalEnvironmentKey("AZURPILOT_NOTIFICATION_AGENT_URL", "infrastructure"),
    LocalEnvironmentKey("AZURPILOT_NOTIFICATION_AGENT_ID", "infrastructure"),
    LocalEnvironmentKey("AZURPILOT_NOTIFICATION_AGENT_PROFILES", "infrastructure"),
    LocalEnvironmentKey(
        "AZURPILOT_NOTIFICATION_AGENT_TOKEN", "infrastructure", secret=True
    ),
    LocalEnvironmentKey(
        "AZURPILOT_NOTIFICATION_AGENT_TOKEN_FILE", "infrastructure"
    ),
    LocalEnvironmentKey(
        "AZURPILOT_NOTIFICATION_AGENT_CURSOR_FILE", "infrastructure"
    ),
    LocalEnvironmentKey(
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
        "infrastructure",
    ),
    LocalEnvironmentKey(
        "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL",
        "infrastructure",
    ),
    LocalEnvironmentKey(
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        "infrastructure",
    ),
    LocalEnvironmentKey(
        "OTEL_EXPORTER_OTLP_METRICS_PROTOCOL",
        "infrastructure",
    ),
    LocalEnvironmentKey(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "infrastructure",
    ),
    LocalEnvironmentKey(
        "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
        "infrastructure",
    ),
    LocalEnvironmentKey("OTEL_RESOURCE_ATTRIBUTES", "infrastructure"),
    LocalEnvironmentKey("OTEL_PYTHON_LOG_HANDLER_LEVEL", "infrastructure"),
    LocalEnvironmentKey("OTEL_METRIC_EXPORT_INTERVAL", "infrastructure"),
    LocalEnvironmentKey("OTEL_BLRP_SCHEDULE_DELAY", "infrastructure"),
    LocalEnvironmentKey("OTEL_BSP_SCHEDULE_DELAY", "infrastructure"),
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

# Эти значения нужны только operator boundary Redis и не должны пересекать
# application runtime boundary даже при общем локальном source `.env`.
APPLICATION_RUNTIME_OPERATOR_ONLY_KEYS = frozenset(
    {
        "AZURPILOT_REDIS_ADMIN_PASSWORD",
        "AZURPILOT_REDISINSIGHT_ENCRYPTION_KEY",
    }
)
if not APPLICATION_RUNTIME_OPERATOR_ONLY_KEYS.issubset(LOCAL_ENVIRONMENT_KEYS):
    raise RuntimeError("Registry локального environment не описывает operator-only keys.")

REDIS_APPLICATION_USERNAME = "azurpilot_app"
REDIS_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
REDIS_SERVICE_HOST = "redis"
REDIS_SERVICE_PORT = 6379


def validate_redis_application_contract(
    *,
    host: str,
    port: str | int,
    username: str,
    password: str,
    allow_service_transport: bool = False,
) -> int:
    """Проверить общий Redis app contract и вернуть нормализованный port."""

    try:
        port_value = int(port)
    except (TypeError, ValueError) as exc:
        raise ValueError("Локальный Docker env содержит некорректный Redis port.") from exc
    service_transport = (
        allow_service_transport
        and host == REDIS_SERVICE_HOST
        and port_value == REDIS_SERVICE_PORT
    )
    if not (host in REDIS_LOOPBACK_HOSTS or service_transport):
        raise ValueError("Локальный Docker env использует недопустимый Redis host.")
    if not 1 <= port_value <= 65_535:
        raise ValueError("Локальный Docker env содержит некорректный Redis port.")
    if username != REDIS_APPLICATION_USERNAME:
        raise ValueError("Локальный Docker env не соответствует Redis app contract.")
    if not password or any(character in password for character in "\x00\r\n"):
        raise ValueError("Локальный Docker env содержит пустой Redis contract value.")
    return port_value


def filter_application_runtime_environment(payload: bytes) -> bytes:
    """Удалить operator-only Redis values из application runtime payload."""

    try:
        text = payload.decode("utf-8")
    except UnicodeError as exc:
        raise RuntimeError("Локальный Docker env невозможно безопасно прочитать.") from exc
    lines: list[str] = []
    seen: set[str] = set()
    for raw_line in text.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        if line.lstrip().startswith("#") or "=" not in line:
            lines.append(raw_line)
            continue
        key, _value = line.split("=", 1)
        key = key.strip()
        if key in APPLICATION_RUNTIME_OPERATOR_ONLY_KEYS:
            if key in seen:
                raise RuntimeError("Локальный Docker env содержит дублирующийся operator key.")
            seen.add(key)
            continue
        lines.append(raw_line)
    return "".join(lines).encode("utf-8")


def get_local_environment_key(name: str) -> LocalEnvironmentKey | None:
    """Вернуть точное описание ключа или ``None`` для неизвестного имени."""

    return _REGISTRY_BY_NAME.get(name)
