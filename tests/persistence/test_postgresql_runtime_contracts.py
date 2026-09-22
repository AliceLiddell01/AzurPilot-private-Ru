from __future__ import annotations

import ast
import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from module.application.canonical_payload import payload_digest
from module.application.commission_recovery import CommissionRecoveryStore
from module.application.errors import StorageConfigurationError, StorageInvalidDataError
from module.application.runtime_cache import get_runtime_cache
from module.application.runtime_storage import RuntimeStorageService
from module.persistence import runtime as persistence_runtime
from module.persistence.config import (
    DEFAULT_BACKEND_MARKER_PATH,
    LEGACY_BACKEND_MARKER_PATH,
    DatabaseSettings,
    migrate_legacy_backend_marker,
)
from module.persistence.local_environment import (
    DEFAULT_LOCAL_ENV_PATH,
    LocalPostgresEnvironment,
)
from module.persistence.redis_runtime_cache import RedisRuntimeCache
from module.persistence.schema import EXPECTED_ALEMBIC_HEAD
from module.statistics import postgresql_stats
from tests.support.paths import REPOSITORY_ROOT
from tests.support.repository.import_inspection import imports_for_path

ROOT = REPOSITORY_ROOT
PRODUCTION_ROOTS = (
    ROOT / "alas.py",
    ROOT / "module" / "application",
    ROOT / "module" / "persistence",
    ROOT / "module" / "statistics",
    ROOT / "module" / "webui",
    ROOT / "module" / "commission",
    ROOT / "module" / "log_res",
    ROOT / "module" / "os",
    ROOT / "module" / "os_handler",
    ROOT / "module" / "os_shop",
    ROOT / "module" / "os_simulator",
)


def _marker_payload() -> dict[str, object]:
    return {
        "backend": "postgresql",
        "version": 1,
        "alembic_head": EXPECTED_ALEMBIC_HEAD,
        "reconciliation_report_sha256": "a" * 64,
        "reviewed_head": "b" * 40,
        "merge_commit": "c" * 40,
        "host": "127.0.0.1",
        "port": 5432,
        "database": "azurpilot",
        "user": "azurpilot_app",
        "sslmode": "disable",
        "runtime_timezone": "Asia/Novosibirsk",
    }


def _local_environment(tmp_path: Path, *, redis_host: str = "127.0.0.1") -> LocalPostgresEnvironment:
    values = {
        "AZURPILOT_POSTGRES_HOST": "127.0.0.1",
        "AZURPILOT_POSTGRES_PORT": "5432",
        "AZURPILOT_POSTGRES_DATABASE": "azurpilot",
        "AZURPILOT_POSTGRES_USER": "azurpilot_app",
        "AZURPILOT_POSTGRES_PASSWORD": "postgres-app-secret",
        "AZURPILOT_POSTGRES_SSLMODE": "disable",
        "AZURPILOT_POSTGRES_RUNTIME_TIMEZONE": "Asia/Novosibirsk",
        "AZURPILOT_POSTGRES_PGPASSFILE": "C:/secure/pgpass.conf",
        "AZURPILOT_POSTGRES_MIGRATOR_HOST": "127.0.0.1",
        "AZURPILOT_POSTGRES_MIGRATOR_PORT": "5432",
        "AZURPILOT_POSTGRES_MIGRATOR_DATABASE": "azurpilot",
        "AZURPILOT_POSTGRES_MIGRATOR_USER": "azurpilot_migrator",
        "AZURPILOT_POSTGRES_MIGRATOR_PASSWORD": "postgres-migrator-secret",
        "AZURPILOT_POSTGRES_MIGRATOR_SSLMODE": "disable",
        "AZURPILOT_POSTGRES_MIGRATOR_RUNTIME_TIMEZONE": "Asia/Novosibirsk",
        "AZURPILOT_POSTGRES_MIGRATOR_PGPASSFILE": "C:/secure/pgpass.conf",
        "AZURPILOT_WSL_DISTRO": "archlinux",
        "AZURPILOT_WSL_PGPASSFILE": "/etc/azurpilot/pgpass",
    }
    infrastructure = {
        "AZURPILOT_REDIS_HOST": redis_host,
        "AZURPILOT_REDIS_PORT": "6379",
        "AZURPILOT_REDIS_USERNAME": "azurpilot_app",
        "AZURPILOT_REDIS_PASSWORD": "redis-app-secret",
    }
    return LocalPostgresEnvironment(
        path=tmp_path / ".env",
        values=values,
        infrastructure_values=infrastructure,
    )


def test_backend_marker_is_required_and_rejects_sqlite(tmp_path: Path):
    marker = tmp_path / "storage_backend.json"

    with pytest.raises(StorageConfigurationError):
        DatabaseSettings.from_backend_marker(marker)

    marker.write_text("{not-json", encoding="utf-8")
    with pytest.raises(StorageConfigurationError):
        DatabaseSettings.from_backend_marker(marker)

    payload = _marker_payload()
    payload["backend"] = "sqlite"
    marker.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(StorageConfigurationError):
        DatabaseSettings.from_backend_marker(marker)

    payload = _marker_payload()
    payload["user"] = "azurpilot_migrator"
    marker.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(StorageConfigurationError):
        DatabaseSettings.from_backend_marker(marker)


def test_backend_marker_has_explicit_identity_time_and_provenance(tmp_path: Path):
    marker = tmp_path / "storage_backend.json"
    marker.write_text(json.dumps(_marker_payload()), encoding="utf-8")

    settings = DatabaseSettings.from_backend_marker(marker)

    assert settings.host == "127.0.0.1"
    assert settings.user == "azurpilot_app"
    assert settings.runtime_timezone == "Asia/Novosibirsk"
    assert settings.password is None


def test_backend_marker_requires_exact_typed_contract(tmp_path: Path):
    marker = tmp_path / "storage_backend.json"
    payload = _marker_payload()
    payload["password"] = "must-never-be-present"
    marker.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(StorageConfigurationError, match="contract"):
        DatabaseSettings.from_backend_marker(marker)

    payload = _marker_payload()
    payload["port"] = True
    marker.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(StorageConfigurationError, match="неполон"):
        DatabaseSettings.from_backend_marker(marker)


def test_backend_marker_default_uses_runtime_state_namespace():
    assert DEFAULT_BACKEND_MARKER_PATH == Path("config/state/storage_backend.json")
    assert LEGACY_BACKEND_MARKER_PATH == Path("config/storage_backend.json")
    assert persistence_runtime._REPOSITORY_ROOT == ROOT


def test_valid_legacy_marker_migrates_create_only(tmp_path: Path):
    target = tmp_path / "config/state/storage_backend.json"
    legacy = tmp_path / "config/storage_backend.json"
    legacy.parent.mkdir()
    legacy.write_text(json.dumps(_marker_payload()), encoding="utf-8")
    before = legacy.read_bytes()

    assert migrate_legacy_backend_marker(target=target, legacy=legacy)

    assert target.read_bytes() == before
    assert not legacy.exists()
    assert DatabaseSettings.from_backend_marker(target).user == "azurpilot_app"


def test_corrupt_legacy_marker_is_not_migrated(tmp_path: Path):
    target = tmp_path / "config/state/storage_backend.json"
    legacy = tmp_path / "config/storage_backend.json"
    legacy.parent.mkdir()
    legacy.write_text(json.dumps({"Alas": {}}), encoding="utf-8")
    before = legacy.read_bytes()

    with pytest.raises(StorageConfigurationError, match="contract"):
        migrate_legacy_backend_marker(target=target, legacy=legacy)

    assert not target.exists()
    assert legacy.read_bytes() == before


def test_broken_legacy_marker_symlink_is_not_treated_as_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target = tmp_path / "config/state/storage_backend.json"
    legacy = tmp_path / "config/storage_backend.json"
    original_exists = Path.exists
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "exists",
        lambda candidate: False if candidate == legacy else original_exists(candidate),
    )
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda candidate: True if candidate == legacy else original_is_symlink(candidate),
    )

    with pytest.raises(StorageConfigurationError, match="небезопасен"):
        migrate_legacy_backend_marker(target=target, legacy=legacy)


def test_legacy_marker_migration_does_not_clobber_racing_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target = tmp_path / "config/state/storage_backend.json"
    legacy = tmp_path / "config/storage_backend.json"
    legacy.parent.mkdir()
    legacy.write_text(json.dumps(_marker_payload()), encoding="utf-8")
    competing = json.dumps({**_marker_payload(), "port": 6543}).encode()
    original = Path.hardlink_to

    def create_competing_target(path: Path, source: Path):
        path.write_bytes(competing)
        return original(path, source)

    monkeypatch.setattr(Path, "hardlink_to", create_competing_target)

    assert not migrate_legacy_backend_marker(target=target, legacy=legacy)
    assert target.read_bytes() == competing
    assert legacy.is_file()


def test_legacy_marker_migration_finishes_same_inode_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target = tmp_path / "config/state/storage_backend.json"
    legacy = tmp_path / "config/storage_backend.json"
    legacy.parent.mkdir()
    legacy.write_text(json.dumps(_marker_payload()), encoding="utf-8")
    original = Path.hardlink_to

    def create_same_target_then_report_race(path: Path, source: Path):
        original(path, source)
        raise FileExistsError

    monkeypatch.setattr(Path, "hardlink_to", create_same_target_then_report_race)

    assert migrate_legacy_backend_marker(target=target, legacy=legacy)
    assert target.is_file()
    assert not legacy.exists()


def test_legacy_marker_migration_keeps_target_when_peer_removes_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target = tmp_path / "config/state/storage_backend.json"
    legacy = tmp_path / "config/storage_backend.json"
    legacy.parent.mkdir()
    legacy.write_text(json.dumps(_marker_payload()), encoding="utf-8")
    expected = legacy.read_bytes()
    original_stat = Path.stat
    legacy_stat_calls = 0

    def remove_legacy_after_target_validation(path: Path, *args, **kwargs):
        nonlocal legacy_stat_calls
        if path == legacy:
            legacy_stat_calls += 1
            if legacy_stat_calls == 3:
                legacy.unlink()
                raise FileNotFoundError
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", remove_legacy_after_target_validation)

    assert migrate_legacy_backend_marker(target=target, legacy=legacy)
    assert target.read_bytes() == expected
    assert not legacy.exists()


def test_runtime_bootstrap_is_lazy_without_health_request(tmp_path: Path):
    marker = tmp_path / "storage_backend.json"
    marker.write_text(json.dumps(_marker_payload()), encoding="utf-8")
    script = f"""
import sys
sys.path.insert(0, {str(ROOT)!r})
from module.persistence.runtime import bootstrap_runtime_storage, dispose_runtime_storage
service = bootstrap_runtime_storage({str(marker)!r}, require_ready=False)
assert service is not None
dispose_runtime_storage()
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("use_custom_path", [False, True])
def test_runtime_bootstrap_binds_redis_provider_to_one_canonical_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_custom_path: bool,
):
    repository_root = tmp_path / "repository"
    marker = repository_root / DEFAULT_BACKEND_MARKER_PATH
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps(_marker_payload()), encoding="utf-8")
    default_path = repository_root / DEFAULT_LOCAL_ENV_PATH
    custom_path = tmp_path / "custom" / ".env"
    custom_path.parent.mkdir()
    expected_path = custom_path if use_custom_path else default_path
    local_environment = _local_environment(expected_path.parent)
    local_environment = LocalPostgresEnvironment(
        path=expected_path,
        values=local_environment.values,
        infrastructure_values=local_environment.infrastructure_values,
    )
    reads: list[Path] = []

    class _Engine:
        def __init__(self, settings: DatabaseSettings) -> None:
            self.settings = settings

        def dispose(self) -> None:
            return None

    monkeypatch.setattr(persistence_runtime, "_REPOSITORY_ROOT", repository_root)
    monkeypatch.setattr(
        persistence_runtime,
        "read_local_postgres_environment",
        lambda path: reads.append(Path(path)) or local_environment,
    )
    monkeypatch.setattr(persistence_runtime, "LazyEngine", _Engine)
    monkeypatch.setattr(persistence_runtime, "_service", None)
    monkeypatch.setattr(persistence_runtime, "_engine", None)
    monkeypatch.setattr(persistence_runtime, "_engine_settings", None)
    monkeypatch.setattr(persistence_runtime, "_runtime_timezone", None)
    for name in (
        "AZURPILOT_LOCAL_ENV_PATH",
        "AZURPILOT_REDIS_HOST",
        "AZURPILOT_REDIS_PORT",
        "AZURPILOT_REDIS_USERNAME",
        "AZURPILOT_REDIS_PASSWORD",
        "AZURPILOT_DOCKER_REDIS_HOST",
        "AZURPILOT_DOCKER_REDIS_PORT",
    ):
        monkeypatch.delenv(name, raising=False)
    if use_custom_path:
        monkeypatch.setenv("AZURPILOT_LOCAL_ENV_PATH", str(custom_path))

    persistence_runtime.bootstrap_runtime_storage(require_ready=False)
    cache = get_runtime_cache()

    assert reads == [expected_path]
    assert isinstance(cache, RedisRuntimeCache)
    assert cache.settings.host == "127.0.0.1"
    assert cache.settings.username == "azurpilot_app"
    assert cache.settings.password
    assert cache._client is None
    assert "AZURPILOT_REDIS_PASSWORD" not in os.environ

    persistence_runtime.dispose_runtime_storage()


def test_runtime_bootstrap_composes_empty_commission_recovery_as_ready_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repository_root = tmp_path / "repository"
    marker = repository_root / DEFAULT_BACKEND_MARKER_PATH
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps(_marker_payload()), encoding="utf-8")
    local_environment = _local_environment(repository_root)

    class _Engine:
        def __init__(self, settings: DatabaseSettings) -> None:
            self.settings = settings

        def dispose(self) -> None:
            return None

    class _RedisClient:
        def __init__(self) -> None:
            self.keys: list[str] = []

        def ping(self) -> bool:
            return True

        def get(self, key: str) -> None:
            self.keys.append(key)

        def close(self) -> None:
            return None

    client = _RedisClient()
    monkeypatch.setattr(persistence_runtime, "_REPOSITORY_ROOT", repository_root)
    monkeypatch.setattr(
        persistence_runtime,
        "read_local_postgres_environment",
        lambda _path: local_environment,
    )
    monkeypatch.setattr(persistence_runtime, "LazyEngine", _Engine)
    monkeypatch.setattr(persistence_runtime, "_service", None)
    monkeypatch.setattr(persistence_runtime, "_engine", None)
    monkeypatch.setattr(persistence_runtime, "_engine_settings", None)
    monkeypatch.setattr(persistence_runtime, "_runtime_timezone", None)
    monkeypatch.setattr(
        RedisRuntimeCache,
        "_client_for_current_process",
        lambda _cache: client,
    )
    for name in (
        "AZURPILOT_LOCAL_ENV_PATH",
        "AZURPILOT_REDIS_HOST",
        "AZURPILOT_REDIS_PORT",
        "AZURPILOT_REDIS_USERNAME",
        "AZURPILOT_REDIS_PASSWORD",
        "AZURPILOT_DOCKER_REDIS_HOST",
        "AZURPILOT_DOCKER_REDIS_PORT",
    ):
        monkeypatch.delenv(name, raising=False)

    persistence_runtime.bootstrap_runtime_storage(require_ready=False)
    store = CommissionRecoveryStore.from_environment()
    try:
        state = store.read("ap")
    finally:
        store.close()

    assert state.status == "unknown"
    assert state.cache_status == "READY"
    assert state.remaining is None
    assert client.keys == ["azurpilot:commission/recovery/ap"]
    persistence_runtime.dispose_runtime_storage()


def test_docker_transport_override_is_ephemeral_and_exact(
    monkeypatch: pytest.MonkeyPatch,
):
    settings = DatabaseSettings(
        host="127.0.0.1",
        port=55432,
        database="azurpilot",
        user="azurpilot_app",
        sslmode="disable",
        runtime_timezone="Asia/Novosibirsk",
    )
    monkeypatch.delenv("AZURPILOT_DOCKER_POSTGRES_HOST", raising=False)
    monkeypatch.delenv("AZURPILOT_DOCKER_POSTGRES_PORT", raising=False)

    assert persistence_runtime._docker_postgres_transport() is None
    assert (
        persistence_runtime._apply_docker_postgres_transport(
            settings, None, None
        ).host
        == "127.0.0.1"
    )

    monkeypatch.setenv("AZURPILOT_DOCKER_POSTGRES_HOST", "postgres")
    monkeypatch.setenv("AZURPILOT_DOCKER_POSTGRES_PORT", "5432")
    transport = persistence_runtime._docker_postgres_transport()
    effective = persistence_runtime._apply_docker_postgres_transport(
        settings, object(), transport
    )

    assert effective.host == "postgres"
    assert effective.port == 5432
    assert settings.host == "127.0.0.1"
    assert settings.port == 55432

    monkeypatch.setenv("AZURPILOT_DOCKER_POSTGRES_HOST", "host.docker.internal")
    with pytest.raises(StorageConfigurationError):
        persistence_runtime._docker_postgres_transport()


def test_runtime_cache_docker_transport_uses_only_canonical_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    local_environment = _local_environment(tmp_path, redis_host="redis")
    monkeypatch.setenv("AZURPILOT_DOCKER_REDIS_HOST", "redis")
    monkeypatch.setenv("AZURPILOT_DOCKER_REDIS_PORT", "6379")

    transport = persistence_runtime._docker_redis_transport()
    settings = persistence_runtime._runtime_cache_settings(
        local_environment,
        transport,
    )

    assert transport is not None
    assert (settings.host, settings.port) == ("redis", 6379)
    monkeypatch.setenv("AZURPILOT_DOCKER_REDIS_HOST", "remote.redis")
    with pytest.raises(StorageConfigurationError):
        persistence_runtime._docker_redis_transport()


def test_database_diagnostics_builds_standalone_read_only_engine_without_production_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / DEFAULT_BACKEND_MARKER_PATH
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(_marker_payload()), encoding="utf-8")

    class _ReadOnlyEnvironment:
        app_passfile = "C:/secure/pgpass.conf"

        def require_app_runtime_match(self, _settings: DatabaseSettings) -> None:
            return None

        def install(self, **_kwargs: object) -> None:
            pytest.fail("Диагностический путь не должен изменять process environment.")

    class _Connection:
        def execute(self, statement: object, _parameters: object = None) -> object:
            sql = str(statement).casefold()
            if "show server_version_num" in sql:
                return SimpleNamespace(scalar_one=lambda: "180000")
            if "version_num from alembic_version" in sql:
                return SimpleNamespace(scalars=lambda: iter([EXPECTED_ALEMBIC_HEAD]))
            if "current_user" in sql:
                return SimpleNamespace(scalar_one_or_none=lambda: "azurpilot_app")
            if "select 1" in sql:
                return SimpleNamespace(scalar_one=lambda: 1)
            raise AssertionError(f"Неожиданный SQL в фикстуре: {sql}")

    class _ConnectionContext:
        def __enter__(self) -> _Connection:
            return _Connection()

        def __exit__(self, *_args: object) -> None:
            return None

    class _DiagnosticEngine:
        def __init__(self, settings: DatabaseSettings) -> None:
            self.settings = settings
            self.connect_calls = 0
            self.disposed = False

        def get(self) -> _DiagnosticEngine:
            return self

        def connect(self) -> _ConnectionContext:
            self.connect_calls += 1
            return _ConnectionContext()

        def dispose(self) -> None:
            self.disposed = True

    engines: list[_DiagnosticEngine] = []

    def _engine(settings: DatabaseSettings) -> _DiagnosticEngine:
        engine = _DiagnosticEngine(settings)
        engines.append(engine)
        return engine

    reads: list[Path] = []

    def _record_read(path: object) -> _ReadOnlyEnvironment:
        reads.append(Path(path))
        return _ReadOnlyEnvironment()

    monkeypatch.setattr(
        persistence_runtime,
        "read_local_postgres_environment",
        _record_read,
    )
    monkeypatch.setattr(persistence_runtime, "LazyEngine", _engine)
    monkeypatch.setattr(persistence_runtime, "_engine", None)
    monkeypatch.setattr(persistence_runtime, "_engine_settings", None)
    monkeypatch.setattr(persistence_runtime, "_service", None)
    from module.application import runtime_storage

    production_provider = object()
    monkeypatch.setattr(runtime_storage, "_provider", production_provider)
    environment_before = dict(os.environ)
    marker_before = marker.read_bytes()
    legacy = tmp_path / LEGACY_BACKEND_MARKER_PATH
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(marker_before)

    diagnostics = persistence_runtime.build_runtime_database_diagnostics(
        SimpleNamespace(repository_root=tmp_path),
    )

    assert reads == [tmp_path / DEFAULT_LOCAL_ENV_PATH]
    assert persistence_runtime.runtime_engine() is None
    assert len(engines) == 1
    assert engines[0].settings.user == "azurpilot_app"
    assert engines[0].settings.passfile == "C:/secure/pgpass.conf"
    assert engines[0].connect_calls == 0
    assert diagnostics.run_check("connectivity", "fixture-target").code == "DEV_DATABASE_CONNECTED"
    assert diagnostics.run_check("app_role", "fixture-target").code == "DEV_DATABASE_APP_ROLE_READY"
    assert engines[0].connect_calls > 0
    assert diagnostics.run_check("config_match", "fixture-target").code == (
        "DEV_DATABASE_CONFIG_MATCH"
    )
    assert dict(os.environ) == environment_before
    assert marker.read_bytes() == marker_before
    assert legacy.read_bytes() == marker_before
    assert runtime_storage._provider is production_provider
    diagnostics.dispose()
    assert engines[0].disposed is True


def test_runtime_idempotency_key_is_stable_inside_observation_window():
    observed_at = datetime(2026, 8, 23, 12, 30, 15, 100, tzinfo=UTC)

    first = RuntimeStorageService._key(
        "commission", "profile", observed_at, (1, (("Cube", 2),))
    )
    retry = RuntimeStorageService._key(
        "commission",
        "profile",
        observed_at.replace(microsecond=900_000),
        (1, (("Cube", 2),)),
    )

    assert first == retry
    assert len(first) <= 128
    assert first != RuntimeStorageService._key(
        "commission",
        "profile",
        observed_at.replace(second=16),
        (1, (("Cube", 2),)),
    )
    assert first != RuntimeStorageService._key(
        "commission", "other-profile", observed_at, (1, (("Cube", 2),))
    )
    assert first != RuntimeStorageService._key(
        "commission", "profile", observed_at, (1, (("Cube", 3),))
    )

    expected = payload_digest(
        {
            "domain": "commission",
            "instance": "profile",
            "observation_window": observed_at.replace(microsecond=0).isoformat(),
            "payload": (Decimal("1.0"), (("Cube", 2),)),
        }
    )
    assert RuntimeStorageService._key(
        "commission", "profile", observed_at, (Decimal("1.0"), (("Cube", 2),))
    ).endswith(expected)


def test_database_settings_wraps_invalid_timezone_value():
    with pytest.raises(StorageConfigurationError, match="Часовой пояс"):
        DatabaseSettings(
            host="127.0.0.1",
            port=5432,
            database="azurpilot",
            user="azurpilot_app",
            runtime_timezone="bad\x00timezone",
        )


def test_resource_snapshot_rejects_unknown_field_before_storage_access():
    service = RuntimeStorageService(
        lambda: pytest.fail("При некорректном поле Unit of Work не открывается.")
    )

    with pytest.raises(StorageInvalidDataError, match="unexpected_resource"):
        service.record_resource_snapshot(
            "profile",
            {"oil": 100, "unexpected_resource": 200},
        )


def test_meow_projection_keeps_all_observed_hazard_levels(monkeypatch):
    monkeypatch.setattr(
        postgresql_stats,
        "get_runtime_storage",
        lambda: SimpleNamespace(
            current_datetime=lambda: datetime(2026, 8, 23, tzinfo=UTC)
        ),
    )
    monkeypatch.setattr(
        postgresql_stats,
        "get_monthly_stats",
        lambda *_args, **_kwargs: {
            "meow_round_times": [{"duration": 12.0, "hazard_level": 4}],
            "meow_battle_times": [],
            "meow_hazard_stats": {
                "2": {
                    "battle_raw_count": 1,
                    "effective_rounds": 0.5,
                    "battle_times": [],
                }
            },
            "siren_research_devices": {"cl1": 0, "meow": {"6": 1}},
            "meow_battle_count": 0.5,
            "meow_battle_raw_count": 1,
        },
    )

    result = postgresql_stats.get_meow_stats("profile", 2026, 8)

    assert tuple(result["by_hazard"]) == ("2", "3", "4", "5", "6")
    assert result["by_hazard"]["4"]["avg_round_time"] == 12.0
    assert result["by_hazard"]["6"]["siren_research_devices"] == 1


def test_production_modules_do_not_import_sqlite_or_legacy_database():
    violations: list[str] = []
    for root in PRODUCTION_ROOTS:
        paths = (root,) if root.is_file() else root.rglob("*.py")
        for path in paths:
            if (ROOT / "module" / "persistence" / "legacy") in path.parents:
                continue
            names = imports_for_path(ROOT, path)
            if any(
                name in {"sqlite3", "module.statistics.cl1_database"}
                or name.startswith(
                    ("sqlite3.", "module.statistics.cl1_database.")
                )
                for name in names
            ):
                violations.append(str(path.relative_to(ROOT)))
    assert not violations, violations

    azurstats = (ROOT / "module" / "statistics" / "azurstats.py").read_text(
        encoding="utf-8"
    )
    assert "load_meowofficer_farming" not in azurstats
    assert "np.loadtxt" not in azurstats


def test_python_services_encode_postgresql_and_lifecycle_ownership():
    lifecycle = (ROOT / "azurpilot" / "tooling" / "lifecycle.py").read_text(
        encoding="utf-8"
    )
    update = (ROOT / "azurpilot" / "tooling" / "update.py").read_text(
        encoding="utf-8"
    )
    repair = (ROOT / "azurpilot" / "tooling" / "repair.py").read_text(
        encoding="utf-8"
    )
    infrastructure = (ROOT / "azurpilot" / "tooling" / "infrastructure.py").read_text(
        encoding="utf-8"
    )

    assert "InfrastructureService" in lifecycle
    assert "ensure_started" in lifecycle
    assert "fetch_branch" in update
    assert "merge_ff_only" in update
    assert "PostgreSqlBackupService" in update
    repair_tree = ast.parse(repair)
    git_calls = [
        node
        for node in ast.walk(repair_tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", getattr(node.func, "id", "")).casefold()
        in {"git", "git_command", "git_run"}
    ]
    assert not git_calls
    assert "JournalStore" in repair
    assert "StructuredProcessRunner" in infrastructure
    assert "def _run_docker" in infrastructure


def test_webui_rejects_database_upload_before_read():
    source = (ROOT / "module" / "webui" / "api.py").read_text(encoding="utf-8")
    rejection = source.index('raise ValueError("LEGACY_DB_UPLOAD_REJECTED")')
    first_read = source.index("await file.read()")
    assert rejection < first_read


def test_webui_legacy_upload_path_is_confined_before_read(tmp_path: Path):
    from module.webui.api import _legacy_upload_target

    (tmp_path / "config").mkdir()
    target, relative = _legacy_upload_target(tmp_path, "old/config/profile.json")
    assert target == tmp_path / "config" / "profile.json"
    assert relative == "config/profile.json"

    with pytest.raises(ValueError, match="LEGACY_DB_UPLOAD_REJECTED"):
        _legacy_upload_target(tmp_path, "old/config/cl1_data.db")
    with pytest.raises(ValueError, match="LEGACY_UPLOAD_PATH_REJECTED"):
        _legacy_upload_target(tmp_path, "old/config/../../outside.json")


def test_webui_validates_all_upload_paths_before_first_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from module.webui.api import api_import_legacy_upload

    class Upload:
        def __init__(self, filename: str):
            self.filename = filename
            self.read_count = 0

        async def read(self) -> bytes:
            self.read_count += 1
            return b"{}"

    class Form:
        def __init__(self, files: list[Upload]):
            self.files = files

        def getlist(self, _name: str) -> list[Upload]:
            return self.files

    class Request:
        def __init__(self, files: list[Upload]):
            self.files = files

        async def form(self) -> Form:
            return Form(self.files)

    valid = Upload("old/config/profile.json")
    rejected = Upload("old/config/cl1_data.db")
    monkeypatch.chdir(tmp_path)

    response = asyncio.run(api_import_legacy_upload(Request([valid, rejected])))

    assert response.status_code == 400
    assert valid.read_count == 0
    assert rejected.read_count == 0
    assert not (tmp_path / "config" / "profile.json").exists()
