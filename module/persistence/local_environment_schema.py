"""Декларативный реестр ключей локального файла `.env` AzurPilot."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

EnvironmentScope = Literal["postgres", "wsl", "infrastructure", "mcp"]


@dataclass(frozen=True, slots=True)
class LocalEnvironmentKey:
    """Описание одного ключа общего локального файла окружения."""

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
    LocalEnvironmentKey(
        "AZURPILOT_GRAFANA_MCP_CALLER_TOKEN",
        "infrastructure",
        secret=True,
    ),
    LocalEnvironmentKey(
        "AZURPILOT_DOCKER_HUB_MCP_CALLER_TOKEN",
        "infrastructure",
        secret=True,
    ),
    LocalEnvironmentKey("DOCKERHUB_USERNAME", "infrastructure"),
    LocalEnvironmentKey("DOCKERHUB_PAT", "infrastructure", secret=True),
    LocalEnvironmentKey(
        "GRAFANA_SERVICE_ACCOUNT_TOKEN",
        "infrastructure",
        secret=True,
    ),
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
        "AZURPILOT_DEV_LOCAL_MCP_TOKEN",
        "mcp",
        secret=True,
    ),
    LocalEnvironmentKey(
        "AZURPILOT_GAME_LOCAL_MCP_TOKEN",
        "mcp",
        secret=True,
    ),
    LocalEnvironmentKey(
        "AZURPILOT_MCP_BRIDGE_CALLER_TOKEN",
        "mcp",
        secret=True,
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
    raise RuntimeError("Реестр локального окружения содержит повторяющийся ключ.")

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
MCP_ENVIRONMENT_KEYS = frozenset(
    entry.name for entry in LOCAL_ENVIRONMENT_REGISTRY if entry.scope == "mcp"
)
SECRET_ENVIRONMENT_KEYS = frozenset(
    entry.name for entry in LOCAL_ENVIRONMENT_REGISTRY if entry.secret
)

# Эти значения нужны только операторской границе Redis и не должны попадать
# в среду выполнения приложения даже при общем локальном источнике `.env`.
APPLICATION_RUNTIME_OPERATOR_ONLY_KEYS = frozenset(
    {
        "AZURPILOT_REDIS_ADMIN_PASSWORD",
        "AZURPILOT_REDISINSIGHT_ENCRYPTION_KEY",
    }
)
if not APPLICATION_RUNTIME_OPERATOR_ONLY_KEYS.issubset(LOCAL_ENVIRONMENT_KEYS):
    raise RuntimeError("В реестре локального окружения не описаны ключи, доступные только оператору.")

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
    """Проверить общий контракт приложения для Redis и вернуть нормализованный порт."""

    try:
        port_value = int(port)
    except (TypeError, ValueError) as exc:
        raise ValueError("Локальное окружение Docker содержит неверный порт Redis.") from exc
    service_transport = (
        allow_service_transport
        and host == REDIS_SERVICE_HOST
        and port_value == REDIS_SERVICE_PORT
    )
    if not (host in REDIS_LOOPBACK_HOSTS or service_transport):
        raise ValueError("Локальное окружение Docker содержит недопустимый адрес Redis.")
    if not 1 <= port_value <= 65_535:
        raise ValueError("Локальное окружение Docker содержит неверный порт Redis.")
    if username != REDIS_APPLICATION_USERNAME:
        raise ValueError("Локальная конфигурация Docker не соответствует контракту приложения Redis.")
    if not password or any(character in password for character in "\x00\r\n"):
        raise ValueError("Локальная конфигурация Docker содержит пустое значение контракта Redis.")
    return port_value


def filter_application_runtime_environment(payload: bytes) -> bytes:
    """Удалить значения Redis только для оператора из данных среды выполнения приложения."""

    try:
        text = payload.decode("utf-8")
    except UnicodeError as exc:
        raise RuntimeError("Файл .env для Docker невозможно безопасно прочитать.") from exc
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
                raise RuntimeError("Локальная конфигурация Docker содержит повторяющийся ключ оператора.")
            seen.add(key)
            continue
        lines.append(raw_line)
    return "".join(lines).encode("utf-8")


def get_local_environment_key(name: str) -> LocalEnvironmentKey | None:
    """Вернуть описание ключа или ``None``, если такое имя неизвестно."""

    return _REGISTRY_BY_NAME.get(name)
