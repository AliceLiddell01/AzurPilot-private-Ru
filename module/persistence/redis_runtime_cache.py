"""Lazy redis-py adapter для ephemeral application runtime cache."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from math import ceil
from pathlib import Path
from typing import Any, ClassVar

from module.application.errors import StorageConfigurationError
from module.application.runtime_cache import (
    RuntimeCacheError,
    RuntimeCacheHealth,
    RuntimeCacheStatus,
)
from module.persistence.local_environment_schema import (
    REDIS_APPLICATION_USERNAME,
    REDIS_SERVICE_HOST,
    REDIS_SERVICE_PORT,
    validate_redis_application_contract,
)

_REDIS_KEYS = frozenset(
    {
        "AZURPILOT_REDIS_HOST",
        "AZURPILOT_REDIS_PORT",
        "AZURPILOT_REDIS_USERNAME",
        "AZURPILOT_REDIS_PASSWORD",
        "AZURPILOT_DOCKER_REDIS_HOST",
        "AZURPILOT_DOCKER_REDIS_PORT",
    }
)
_APPLICATION_KEY_PREFIX = "azurpilot:"


def _parse_env_file(path: Path) -> dict[str, str]:
    """Прочитать Redis keys через canonical защищённый local environment."""
    try:
        from module.persistence.local_environment import read_local_postgres_environment

        local = read_local_postgres_environment(path)
    except StorageConfigurationError:
        raise
    except (OSError, UnicodeError) as exc:
        raise StorageConfigurationError("Локальный Redis env невозможно прочитать.") from exc
    if local is None:
        raise StorageConfigurationError("Локальный Redis env отсутствует или небезопасен.")
    return {
        key: local.infrastructure_values[key]
        for key in _REDIS_KEYS
        if key in local.infrastructure_values
    }


@dataclass(frozen=True, slots=True)
class RuntimeCacheSettings:
    """Ограниченные Redis transport settings без credentials в URL."""

    host: str
    port: int
    username: str
    password: str = field(repr=False)
    key_prefix: ClassVar[str] = _APPLICATION_KEY_PREFIX
    socket_connect_timeout_seconds: float = 1.0
    socket_timeout_seconds: float = 1.0

    def __post_init__(self) -> None:
        if not self.host or any(character.isspace() for character in self.host):
            raise StorageConfigurationError("Redis host некорректен.")
        if not 1 <= self.port <= 65_535:
            raise StorageConfigurationError("Redis port некорректен.")
        if self.username != REDIS_APPLICATION_USERNAME or any(
            character.isspace() for character in self.username
        ):
            raise StorageConfigurationError("Redis username некорректен.")
        if not self.password or any(character in self.password for character in "\x00\r\n"):
            raise StorageConfigurationError("Redis password некорректен.")
        for value in (
            self.socket_connect_timeout_seconds,
            self.socket_timeout_seconds,
        ):
            if not 0.05 <= value <= 10:
                raise StorageConfigurationError("Redis timeout должен быть от 0.05 до 10 секунд.")

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        *,
        env_file: str | Path | None = None,
    ) -> RuntimeCacheSettings:
        """Загрузить settings из mapping и, при необходимости, staged `.env`."""

        source = dict(os.environ if environment is None else environment)
        configured_path = env_file or source.get("AZURPILOT_LOCAL_ENV_PATH")
        if configured_path is not None:
            source = {**_parse_env_file(Path(configured_path)), **source}

        docker_host = source.get("AZURPILOT_DOCKER_REDIS_HOST")
        docker_port = source.get("AZURPILOT_DOCKER_REDIS_PORT")
        if (docker_host is None) != (docker_port is None):
            raise StorageConfigurationError(
                "Docker Redis transport должен задавать host и port вместе."
            )
        if docker_host is not None:
            if docker_host != REDIS_SERVICE_HOST or docker_port != str(REDIS_SERVICE_PORT):
                raise StorageConfigurationError(
                    "Docker Redis transport не соответствует canonical Compose service."
                )
            host, port = docker_host, REDIS_SERVICE_PORT
        else:
            host = source.get("AZURPILOT_REDIS_HOST")
            if not host:
                raise StorageConfigurationError("Переменная AZURPILOT_REDIS_HOST не задана.")
            port = source.get("AZURPILOT_REDIS_PORT", "6379")

        username = source.get("AZURPILOT_REDIS_USERNAME")
        password = source.get("AZURPILOT_REDIS_PASSWORD")
        if not username or not password:
            raise StorageConfigurationError("Redis app credentials не настроены.")
        try:
            port = validate_redis_application_contract(
                host=host,
                port=port,
                username=username,
                password=password,
                allow_service_transport=docker_host is not None,
            )
        except ValueError as exc:
            raise StorageConfigurationError(str(exc)) from exc
        return cls(host=host, port=port, username=username, password=password)


class RedisRuntimeCache:
    """Redis cache с lazy, fork-safe и bounded transport lifecycle."""

    def __init__(self, settings: RuntimeCacheSettings) -> None:
        self.settings = settings
        self._client: Any | None = None
        self._client_pid: int | None = None

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        *,
        env_file: str | Path | None = None,
    ) -> RedisRuntimeCache:
        try:
            settings = RuntimeCacheSettings.from_environment(
                environment, env_file=env_file
            )
        except StorageConfigurationError as exc:
            raise RuntimeCacheError(RuntimeCacheStatus.NOT_CONFIGURED) from exc
        return cls(settings)

    def _client_for_current_process(self) -> Any:
        current_pid = os.getpid()
        if self._client is not None and self._client_pid != current_pid:
            self._close_client()
        if self._client is None:
            try:
                import redis
                from redis.backoff import NoBackoff
                from redis.retry import Retry

                self._client = redis.Redis(
                    host=self.settings.host,
                    port=self.settings.port,
                    username=self.settings.username,
                    password=self.settings.password,
                    socket_connect_timeout=self.settings.socket_connect_timeout_seconds,
                    socket_timeout=self.settings.socket_timeout_seconds,
                    retry=Retry(NoBackoff(), retries=0),
                    health_check_interval=30,
                    decode_responses=False,
                )
            except Exception as exc:  # noqa: BLE001 - provider boundary maps unknown failures
                raise self._error_for(exc) from None
            self._client_pid = current_pid
        return self._client

    def _close_client(self) -> None:
        client, self._client = self._client, None
        self._client_pid = None
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001, S110 - close is best-effort cleanup
                pass

    @staticmethod
    def _error_for(error: BaseException) -> RuntimeCacheError:
        try:
            import redis

            if isinstance(
                error,
                (
                    redis.exceptions.AuthenticationError,
                    redis.exceptions.NoPermissionError,
                    redis.exceptions.AuthorizationError,
                ),
            ):
                status = RuntimeCacheStatus.AUTH_FAILED
            elif isinstance(error, (redis.exceptions.TimeoutError, TimeoutError)):
                status = RuntimeCacheStatus.TIMEOUT
            elif isinstance(error, redis.exceptions.ConnectionError):
                status = RuntimeCacheStatus.UNAVAILABLE
            elif isinstance(error, redis.exceptions.ResponseError):
                status = RuntimeCacheStatus.INVALID_DATA
            else:
                status = RuntimeCacheStatus.UNKNOWN
        except ImportError:
            status = RuntimeCacheStatus.UNKNOWN
        return RuntimeCacheError(status)

    def _key(self, key: str) -> str:
        if (
            not isinstance(key, str)
            or not key
            or len(key) > 256
            or key.startswith(_APPLICATION_KEY_PREFIX)
            or any(character.isspace() or character == "\x00" for character in key)
        ):
            raise RuntimeCacheError(RuntimeCacheStatus.INVALID_DATA)
        return _APPLICATION_KEY_PREFIX + key

    @staticmethod
    def _value(value: bytes) -> bytes:
        if not isinstance(value, bytes) or not value:
            raise RuntimeCacheError(RuntimeCacheStatus.INVALID_DATA)
        if len(value) > 4 * 1024 * 1024:
            raise RuntimeCacheError(RuntimeCacheStatus.INVALID_DATA)
        return value

    def health(self) -> RuntimeCacheHealth:
        try:
            self._client_for_current_process().ping()
        except RuntimeCacheError as error:
            return RuntimeCacheHealth(status=error.status)
        except Exception as exc:  # noqa: BLE001 - provider boundary maps unknown failures
            error = self._error_for(exc)
            return RuntimeCacheHealth(status=error.status)
        return RuntimeCacheHealth(status=RuntimeCacheStatus.READY)

    def get(self, key: str) -> bytes | None:
        try:
            value = self._client_for_current_process().get(self._key(key))
        except RuntimeCacheError:
            raise
        except Exception as exc:  # noqa: BLE001 - provider boundary maps unknown failures
            raise self._error_for(exc) from None
        if value is None:
            return None
        if not isinstance(value, bytes):
            raise RuntimeCacheError(RuntimeCacheStatus.INVALID_DATA)
        return value

    def set(
        self,
        key: str,
        value: bytes,
        *,
        expires_at: datetime | None = None,
    ) -> None:
        encoded = self._value(value)
        if expires_at is not None:
            if expires_at.tzinfo is None:
                raise RuntimeCacheError(RuntimeCacheStatus.INVALID_DATA)
            expires_at = expires_at.astimezone(UTC)
            now = datetime.now(UTC)
            if expires_at <= now:
                raise RuntimeCacheError(RuntimeCacheStatus.INVALID_DATA)
            expiration = ceil(expires_at.timestamp())
        else:
            expiration = None
        try:
            result = self._client_for_current_process().set(
                self._key(key), encoded, exat=expiration
            )
        except RuntimeCacheError:
            raise
        except Exception as exc:  # noqa: BLE001 - provider boundary maps unknown failures
            raise self._error_for(exc) from None
        if result is not True:
            raise RuntimeCacheError(RuntimeCacheStatus.UNKNOWN)

    def delete(self, key: str) -> bool:
        try:
            deleted = self._client_for_current_process().delete(self._key(key))
        except RuntimeCacheError:
            raise
        except Exception as exc:  # noqa: BLE001 - provider boundary maps unknown failures
            raise self._error_for(exc) from None
        if not isinstance(deleted, int) or deleted < 0:
            raise RuntimeCacheError(RuntimeCacheStatus.INVALID_DATA)
        return deleted > 0

    def close(self) -> None:
        self._close_client()


__all__ = ("RedisRuntimeCache", "RuntimeCacheSettings")
