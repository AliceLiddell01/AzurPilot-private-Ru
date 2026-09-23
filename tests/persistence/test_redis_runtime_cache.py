from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from math import ceil
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import ClassVar

import pytest

from module.application.commission_recovery import CommissionRecoveryStore
from module.application.runtime_cache import RuntimeCacheError, RuntimeCacheStatus
from module.persistence import redis_runtime_cache
from module.persistence.redis_runtime_cache import (
    RedisRuntimeCache,
    RuntimeCacheSettings,
)


class _AuthenticationError(Exception):
    pass


class _TimeoutError(Exception):
    pass


class _ConnectionError(Exception):
    pass


class _ResponseError(Exception):
    pass


class _NoPermissionError(_ResponseError):
    pass


class _AuthorizationError(_ConnectionError):
    pass


class _NoBackoff:
    pass


class _Retry:
    def __init__(self, backoff: object, retries: int) -> None:
        self.backoff = backoff
        self.retries = retries


class _FakeRedis:
    instances: ClassVar[list[_FakeRedis]] = []
    ping_error: BaseException | None = None
    get_error: BaseException | None = None

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.values: dict[str, bytes] = {}
        self.set_calls: list[tuple[str, bytes, int | None]] = []
        self.closed = False
        self.__class__.instances.append(self)

    def ping(self) -> bool:
        if self.__class__.ping_error is not None:
            raise self.__class__.ping_error
        return True

    def get(self, key: str) -> bytes | None:
        if self.__class__.get_error is not None:
            raise self.__class__.get_error
        return self.values.get(key)

    def set(self, key: str, value: bytes, *, exat: int | None = None) -> bool:
        self.values[key] = value
        self.set_calls.append((key, value, exat))
        return True

    def delete(self, key: str) -> int:
        return int(self.values.pop(key, None) is not None)

    def close(self) -> None:
        self.closed = True


def _install_fake_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeRedis.instances = []
    _FakeRedis.ping_error = None
    _FakeRedis.get_error = None
    module = ModuleType("redis")
    module.Redis = _FakeRedis  # type: ignore[attr-defined]
    module.exceptions = SimpleNamespace(
        AuthenticationError=_AuthenticationError,
        NoPermissionError=_NoPermissionError,
        AuthorizationError=_AuthorizationError,
        TimeoutError=_TimeoutError,
        ConnectionError=_ConnectionError,
        ResponseError=_ResponseError,
    )
    backoff = ModuleType("redis.backoff")
    backoff.NoBackoff = _NoBackoff  # type: ignore[attr-defined]
    retry = ModuleType("redis.retry")
    retry.Retry = _Retry  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "redis.backoff", backoff)
    monkeypatch.setitem(sys.modules, "redis.retry", retry)
    monkeypatch.setitem(sys.modules, "redis", module)


def _settings() -> RuntimeCacheSettings:
    return RuntimeCacheSettings(
        host="127.0.0.1",
        port=6379,
        username="azurpilot_app",
        password="app-secret",
    )


def _write_secure_env(path: Path) -> None:
    path.write_text(
        """AZURPILOT_POSTGRES_HOST=127.0.0.1
AZURPILOT_POSTGRES_PORT=5432
AZURPILOT_POSTGRES_DATABASE=azurpilot
AZURPILOT_POSTGRES_USER=azurpilot_app
AZURPILOT_POSTGRES_PASSWORD=app-secret
AZURPILOT_POSTGRES_SSLMODE=disable
AZURPILOT_POSTGRES_RUNTIME_TIMEZONE=Asia/Novosibirsk
AZURPILOT_POSTGRES_PGPASSFILE=C:/secure/pgpass.conf
AZURPILOT_POSTGRES_MIGRATOR_HOST=127.0.0.1
AZURPILOT_POSTGRES_MIGRATOR_PORT=5432
AZURPILOT_POSTGRES_MIGRATOR_DATABASE=azurpilot
AZURPILOT_POSTGRES_MIGRATOR_USER=azurpilot_migrator
AZURPILOT_POSTGRES_MIGRATOR_PASSWORD=migrator-secret
AZURPILOT_POSTGRES_MIGRATOR_SSLMODE=disable
AZURPILOT_POSTGRES_MIGRATOR_RUNTIME_TIMEZONE=Asia/Novosibirsk
AZURPILOT_POSTGRES_MIGRATOR_PGPASSFILE=C:/secure/pgpass.conf
AZURPILOT_WSL_DISTRO=archlinux
AZURPILOT_WSL_PGPASSFILE=/etc/azurpilot/pgpass
AZURPILOT_REDIS_HOST=127.0.0.1
AZURPILOT_REDIS_PORT=6379
AZURPILOT_REDIS_USERNAME=azurpilot_app
AZURPILOT_REDIS_PASSWORD=redis-app-secret
AZURPILOT_REDIS_ADMIN_PASSWORD=redis-admin-secret
AZURPILOT_REDISINSIGHT_ENCRYPTION_KEY=redis-insight-key
AZURPILOT_REDISINSIGHT_PORT=5540
""",
        encoding="utf-8",
    )
    if os.name == "nt":
        identity = subprocess.run(
            ["whoami.exe"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(
            [
                "icacls.exe",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"{identity}:(F)",
                "/grant:r",
                "SYSTEM:(F)",
            ],
            check=True,
            capture_output=True,
        )
    else:
        path.chmod(0o600)


def test_runtime_cache_settings_read_staged_env_file(tmp_path: Path):
    env_file = tmp_path / ".env"
    _write_secure_env(env_file)

    settings = RuntimeCacheSettings.from_environment(
        {
            "AZURPILOT_DOCKER_REDIS_HOST": "redis",
            "AZURPILOT_DOCKER_REDIS_PORT": "6379",
        },
        env_file=env_file,
    )

    assert (settings.host, settings.port) == ("redis", 6379)
    assert settings.username == "azurpilot_app"
    assert settings.password == "redis-app-secret"


def test_runtime_cache_is_lazy_namespaced_and_uses_absolute_expiry(
    monkeypatch: pytest.MonkeyPatch,
):
    _install_fake_redis(monkeypatch)
    cache = RedisRuntimeCache(_settings())

    assert _FakeRedis.instances == []
    assert cache.health().status is RuntimeCacheStatus.READY
    expires_at = datetime.now(UTC) + timedelta(minutes=5)

    cache.set("task-state", b"value", expires_at=expires_at)

    client = _FakeRedis.instances[0]
    assert client.kwargs["retry"].retries == 0
    assert client.set_calls[0][0] == "azurpilot:task-state"
    assert client.set_calls[0][1] == b"value"
    assert client.set_calls[0][2] == ceil(expires_at.timestamp())
    assert cache.get("task-state") == b"value"
    assert cache.get("missing") is None
    assert cache.delete("task-state") is True
    assert cache.delete("task-state") is False


def test_runtime_cache_recreates_client_after_pid_change(monkeypatch: pytest.MonkeyPatch):
    _install_fake_redis(monkeypatch)
    original_getpid = redis_runtime_cache.os.getpid
    pid_calls = 0

    def fake_getpid() -> int:
        nonlocal pid_calls
        pid_calls += 1
        if pid_calls == 1:
            return 100
        if pid_calls == 2:
            return 200
        return original_getpid()

    monkeypatch.setattr(redis_runtime_cache.os, "getpid", fake_getpid)
    cache = RedisRuntimeCache(_settings())

    assert cache.health().available
    assert cache.health().available
    assert len(_FakeRedis.instances) == 2
    assert _FakeRedis.instances[0].closed


@pytest.mark.parametrize(
    ("error", "expected"),
    (
        (_AuthenticationError("provider-secret"), RuntimeCacheStatus.AUTH_FAILED),
        (_NoPermissionError("provider-secret"), RuntimeCacheStatus.AUTH_FAILED),
        (_AuthorizationError("provider-secret"), RuntimeCacheStatus.AUTH_FAILED),
        (_TimeoutError("provider-secret"), RuntimeCacheStatus.TIMEOUT),
        (_ConnectionError("provider-secret"), RuntimeCacheStatus.UNAVAILABLE),
        (_ResponseError("provider-secret"), RuntimeCacheStatus.INVALID_DATA),
    ),
)
def test_runtime_cache_health_maps_provider_errors_without_payload_leak(
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
    expected: RuntimeCacheStatus,
):
    _install_fake_redis(monkeypatch)
    _FakeRedis.ping_error = error
    cache = RedisRuntimeCache(_settings())

    health = cache.health()

    assert health.status is expected
    assert "provider-secret" not in str(RuntimeCacheError(expected))


def test_runtime_cache_maps_pinned_redis_acl_exception_hierarchy():
    import redis

    assert issubclass(redis.exceptions.NoPermissionError, redis.exceptions.ResponseError)
    assert issubclass(redis.exceptions.AuthorizationError, redis.exceptions.ConnectionError)
    assert redis_runtime_cache.RedisRuntimeCache._error_for(
        redis.exceptions.NoPermissionError("provider-secret")
    ).status is RuntimeCacheStatus.AUTH_FAILED
    assert redis_runtime_cache.RedisRuntimeCache._error_for(
        redis.exceptions.AuthorizationError("provider-secret")
    ).status is RuntimeCacheStatus.AUTH_FAILED


def test_runtime_cache_rejects_unscoped_keys_and_invalid_expiry(
    monkeypatch: pytest.MonkeyPatch,
):
    _install_fake_redis(monkeypatch)
    cache = RedisRuntimeCache(_settings())

    with pytest.raises(RuntimeCacheError) as key_error:
        cache.get("azurpilot:already-prefixed")
    assert key_error.value.status is RuntimeCacheStatus.INVALID_DATA

    with pytest.raises(RuntimeCacheError) as value_error:
        cache.set("empty", b"")
    assert value_error.value.status is RuntimeCacheStatus.INVALID_DATA

    with pytest.raises(RuntimeCacheError) as expiry_error:
        cache.set("expired", b"value", expires_at=datetime.now(UTC) - timedelta(seconds=1))
    assert expiry_error.value.status is RuntimeCacheStatus.INVALID_DATA


def test_runtime_cache_settings_use_canonical_docker_transport():
    settings = RuntimeCacheSettings.from_environment(
        {
            "AZURPILOT_REDIS_HOST": "127.0.0.1",
            "AZURPILOT_REDIS_PORT": "55432",
            "AZURPILOT_REDIS_USERNAME": "azurpilot_app",
            "AZURPILOT_REDIS_PASSWORD": "app-secret",
            "AZURPILOT_DOCKER_REDIS_HOST": "redis",
            "AZURPILOT_DOCKER_REDIS_PORT": "6379",
        }
    )

    assert (settings.host, settings.port) == ("redis", 6379)


def test_runtime_cache_factory_distinguishes_missing_configuration():
    with pytest.raises(RuntimeCacheError) as error:
        RedisRuntimeCache.from_environment({})

    assert error.value.status is RuntimeCacheStatus.NOT_CONFIGURED


def test_commission_recovery_uses_physical_application_namespace(
    monkeypatch: pytest.MonkeyPatch,
):
    _install_fake_redis(monkeypatch)
    cache = RedisRuntimeCache(_settings())
    store = CommissionRecoveryStore(
        cache,
        now=lambda: datetime.now(UTC) + timedelta(days=1),
    )

    observed = store.record_observation("ap", 4)
    read_back = store.read("ap")

    assert observed.status == "confirmed"
    assert read_back.status == "confirmed"
    assert read_back.remaining == 4
    assert read_back.used == 1
    client = _FakeRedis.instances[0]
    assert client.set_calls[0][0] == "azurpilot:commission/recovery/ap"
    assert client.set_calls[0][2] == ceil(observed.reset_at.timestamp())
    store.close()
