from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from module.application.errors import (
    StorageConfigurationError,
    StorageConfigurationUnknownError,
)
from module.persistence import local_environment as local_environment_module
from module.persistence.config import DatabaseSettings
from module.persistence.local_environment import (
    LocalPostgresEnvironment,
    load_local_postgres_environment,
    read_local_environment_subset,
)
from module.persistence.local_environment_schema import SECRET_ENVIRONMENT_KEYS


def _document() -> str:
    return """AZURPILOT_POSTGRES_HOST=127.0.0.1
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
"""


def _write_env(path: Path, document: str) -> None:
    path.write_text(document, encoding="utf-8")
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


def test_local_env_installs_metadata_and_passfile_without_secret_environment(
    tmp_path: Path,
):
    path = tmp_path / ".env"
    _write_env(path, _document())
    environment = {"PGPASSWORD": "stale", "AZURPILOT_POSTGRES_PASSWORD": "stale"}

    local = load_local_postgres_environment(path, environment=environment)

    assert local is not None
    assert environment["AZURPILOT_POSTGRES_USER"] == "azurpilot_app"
    assert environment["PGPASSFILE"] == "C:/secure/pgpass.conf"
    assert "PGPASSWORD" not in environment
    assert "AZURPILOT_POSTGRES_PASSWORD" not in environment
    assert "AZURPILOT_POSTGRES_MIGRATOR_PASSWORD" not in environment


def test_local_env_ignores_reserved_observability_namespace(tmp_path: Path):
    path = tmp_path / ".env"
    _write_env(
        path,
        _document()
        + "AZURPILOT_OBSERVABILITY_GRAFANA_ADMIN_USER=admin\n"
        + "AZURPILOT_OBSERVABILITY_GRAFANA_ADMIN_PASSWORD=observability-secret\n",
    )
    environment: dict[str, str] = {}

    local = load_local_postgres_environment(path, environment=environment)

    assert local is not None
    assert not any(key.startswith("AZURPILOT_OBSERVABILITY_") for key in local.values)
    assert not any(
        key.startswith("AZURPILOT_OBSERVABILITY_") for key in environment
    )


def test_local_env_ignores_reserved_docker_namespace(tmp_path: Path):
    path = tmp_path / ".env"
    _write_env(
        path,
        _document() + "AZURPILOT_POSTGRES_DOCKER_BOOTSTRAP_PASSWORD=docker-secret\n",
    )
    environment: dict[str, str] = {}

    local = load_local_postgres_environment(path, environment=environment)

    assert local is not None
    assert "AZURPILOT_POSTGRES_DOCKER_BOOTSTRAP_PASSWORD" not in local.values
    assert "AZURPILOT_POSTGRES_DOCKER_BOOTSTRAP_PASSWORD" not in environment


def test_local_env_accepts_exact_infrastructure_registry_keys(tmp_path: Path):
    path = tmp_path / ".env"
    _write_env(
        path,
        _document()
        + "AZURPILOT_POSTGRES_DOCKER_BOOTSTRAP_PASSWORD=docker-secret\n"
        + "AZURPILOT_OBSERVABILITY_PGADMIN_ADMIN_EMAIL=operator@example.test\n"
        + "AZURPILOT_OBSERVABILITY_PGADMIN_PORT=5051\n"
        + "AZURPILOT_CADDY_HOST=mcp.example.test\n"
        + "AZURPILOT_GAME_MCP_PUBLIC_HOST=game.mcp.example.test\n",
    )

    local = load_local_postgres_environment(path, environment={})

    assert local is not None


def test_local_env_accepts_canonical_otlp_configuration_without_exporting_it(
    tmp_path: Path,
):
    path = tmp_path / ".env"
    otlp = (
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=http://127.0.0.1:4318/v1/logs\n"
        "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL=http/protobuf\n"
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT=http://127.0.0.1:4318/v1/metrics\n"
        "OTEL_EXPORTER_OTLP_METRICS_PROTOCOL=http/protobuf\n"
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://127.0.0.1:4318/v1/traces\n"
        "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/protobuf\n"
        "OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=local\n"
        "OTEL_PYTHON_LOG_HANDLER_LEVEL=INFO\n"
        "OTEL_METRIC_EXPORT_INTERVAL=1000\n"
        "OTEL_BLRP_SCHEDULE_DELAY=500\n"
        "OTEL_BSP_SCHEDULE_DELAY=500\n"
    )
    _write_env(path, _document() + otlp)
    environment: dict[str, str] = {}

    local = load_local_postgres_environment(path, environment=environment)

    assert local is not None
    assert not any(key.startswith("OTEL_") for key in local.values)
    assert not any(key.startswith("OTEL_") for key in environment)


def test_local_env_rejects_bare_docker_namespace_key(tmp_path: Path):
    path = tmp_path / ".env"
    _write_env(path, _document() + "AZURPILOT_POSTGRES_DOCKER_=value\n")

    with pytest.raises(StorageConfigurationError, match="Ключ"):
        load_local_postgres_environment(path, environment={})


@pytest.mark.parametrize(
    "key",
    (
        "AZURPILOT_POSTGRES_DOCKER_BOOTSTRP_PASSWORD",
        "AZURPILOT_OBSERVABILITY_PGADMIN_PORTX",
        "AZURPILOT_CADDY_HOSTX",
        "AZURPILOT_GAME_MCP_PUBLIC_HOSTX",
        "AZURPILOT_REDIS_PASWORD",
    ),
)
def test_local_env_rejects_typo_inside_infrastructure_namespace(
    tmp_path: Path, key: str
):
    path = tmp_path / ".env"
    _write_env(path, _document() + f"{key}=value\n")

    with pytest.raises(StorageConfigurationError, match="Ключ"):
        load_local_postgres_environment(path, environment={})


def test_local_env_can_select_migrator_without_exporting_secret(tmp_path: Path):
    path = tmp_path / ".env"
    _write_env(path, _document())
    environment: dict[str, str] = {}

    load_local_postgres_environment(path, role="migrator", environment=environment)

    assert environment["AZURPILOT_POSTGRES_USER"] == "azurpilot_migrator"
    assert environment["PGPASSFILE"] == "C:/secure/pgpass.conf"
    assert (
        environment["AZURPILOT_POSTGRES_PGPASSFILE"]
        == "C:/secure/pgpass.conf"
    )
    assert all("secret" not in value for value in environment.values())


def test_local_env_rejects_unknown_or_duplicate_contract_key(tmp_path: Path):
    path = tmp_path / ".env"
    _write_env(path, _document() + "AZURPILOT_POSTGRES_UNUSED=value\n")
    with pytest.raises(StorageConfigurationError, match="Ключ"):
        load_local_postgres_environment(path, environment={})

    _write_env(path, _document().replace("app-secret", "app-secret #comment", 1))
    with pytest.raises(StorageConfigurationError, match="строке"):
        load_local_postgres_environment(path, environment={})

    _write_env(path, _document() + "AZURPILOT_POSTGRES_HOST=localhost\n")
    with pytest.raises(StorageConfigurationError, match="Ключ"):
        load_local_postgres_environment(path, environment={})

    _write_env(
        path,
        _document()
        + "AZURPILOT_OBSERVABILITY_GRAFANA_ADMIN_USER=admin\n"
        + "AZURPILOT_OBSERVABILITY_GRAFANA_ADMIN_USER=duplicate\n",
    )
    with pytest.raises(StorageConfigurationError, match="Ключ"):
        load_local_postgres_environment(path, environment={})


def test_local_env_requires_distinct_secrets_and_full_contract(tmp_path: Path):
    path = tmp_path / ".env"
    _write_env(path, _document().replace("migrator-secret", "app-secret"))
    with pytest.raises(StorageConfigurationError, match="разные"):
        load_local_postgres_environment(path, environment={})

    _write_env(
        path,
        _document().replace("AZURPILOT_POSTGRES_PORT=5432\n", "", 1),
    )
    with pytest.raises(StorageConfigurationError, match="полного производственного контракта"):
        load_local_postgres_environment(path, environment={})


def test_local_env_requires_exact_production_roles(tmp_path: Path):
    path = tmp_path / ".env"
    _write_env(path, _document().replace("azurpilot_app", "postgres", 1))

    with pytest.raises(StorageConfigurationError, match="Роль"):
        load_local_postgres_environment(path, environment={})

    _write_env(path, _document().replace("azurpilot_migrator", "postgres", 1))
    with pytest.raises(StorageConfigurationError, match="Роль"):
        load_local_postgres_environment(path, environment={})


def test_local_env_runtime_contract_must_match_marker(tmp_path: Path):
    path = tmp_path / ".env"
    _write_env(path, _document())
    local = load_local_postgres_environment(path, environment={})
    assert local is not None
    settings = DatabaseSettings(
        host="127.0.0.1",
        port=5432,
        database="azurpilot",
        user="azurpilot_app",
        sslmode="disable",
        runtime_timezone="Asia/Novosibirsk",
    )
    local.require_app_runtime_match(settings)

    with pytest.raises(StorageConfigurationError, match="не совпадает"):
        local.require_app_runtime_match(
            DatabaseSettings(
                host="127.0.0.1",
                port=5433,
                database="azurpilot",
                user="azurpilot_app",
                sslmode="disable",
                runtime_timezone="Asia/Novosibirsk",
            )
        )


def test_missing_local_env_is_a_noop(tmp_path: Path):
    environment = os.environ.copy()
    assert load_local_postgres_environment(
        tmp_path / ".env", environment=environment
    ) is None


def test_direct_local_environment_rejects_incomplete_contract(tmp_path: Path):
    with pytest.raises(StorageConfigurationError, match="полного производственного контракта"):
        LocalPostgresEnvironment(path=tmp_path / ".env", values={})


def test_direct_local_environment_rejects_extra_contract_key(tmp_path: Path):
    values = dict(line.split("=", 1) for line in _document().splitlines() if line)
    values["UNEXPECTED"] = "value"
    with pytest.raises(StorageConfigurationError, match="полного производственного контракта"):
        LocalPostgresEnvironment(path=tmp_path / ".env", values=values)


def test_local_env_requires_matching_app_and_migrator_endpoint(tmp_path: Path):
    path = tmp_path / ".env"
    _write_env(
        path,
        _document().replace(
            "AZURPILOT_POSTGRES_MIGRATOR_DATABASE=azurpilot",
            "AZURPILOT_POSTGRES_MIGRATOR_DATABASE=other",
        ),
    )
    with pytest.raises(StorageConfigurationError, match="конечные точки PostgreSQL"):
        load_local_postgres_environment(path, environment={})


def test_local_env_rejects_broad_permissions(tmp_path: Path, monkeypatch):
    path = tmp_path / ".env"
    _write_env(path, _document())
    if os.name == "nt":
        monkeypatch.setattr(
            "module.persistence.local_environment._windows_acl_is_restricted",
            lambda _path: False,
        )
    else:
        path.chmod(0o644)

    with pytest.raises(StorageConfigurationError, match="права доступа"):
        load_local_postgres_environment(path, environment={})


def test_local_env_read_failure_is_an_unknown_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".env"
    _write_env(path, _document())
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PermissionError("synthetic access denial")
        ),
    )

    with pytest.raises(StorageConfigurationUnknownError):
        local_environment_module._read_local_environment_values(path)


def test_local_env_read_race_is_an_unknown_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".env"
    _write_env(path, _document())
    original_read_text = Path.read_text

    def read_and_change(candidate: Path, *args, **kwargs):
        contents = original_read_text(candidate, *args, **kwargs)
        if candidate == path:
            path.write_text(contents + "\n# changed during read", encoding="utf-8")
        return contents

    monkeypatch.setattr(Path, "read_text", read_and_change)

    with pytest.raises(StorageConfigurationUnknownError, match="изменилось во время чтения"):
        read_local_environment_subset(path, keys=("AZURPILOT_POSTGRES_HOST",))


def test_windows_acl_probe_does_not_inherit_registered_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in SECRET_ENVIRONMENT_KEYS:
        monkeypatch.setenv(key, f"test-only-{key}")
    monkeypatch.setenv("PGPASSWORD", "test-only-PGPASSWORD")
    original_values = {
        key: os.environ[key] for key in (*SECRET_ENVIRONMENT_KEYS, "PGPASSWORD")
    }
    captured: dict[str, str] = {}
    current_sid = "S-1-5-21-1000"

    def run(_args, **kwargs):
        captured.update(kwargs["env"])
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "CurrentSid": current_sid,
                    "OwnerSid": current_sid,
                    "Protected": True,
                    "Rules": [
                        {
                            "Sid": current_sid,
                            "Rights": 0x1F01FF,
                            "Type": "Allow",
                            "Inherited": False,
                        }
                    ],
                }
            ),
        )

    monkeypatch.setattr(
        local_environment_module.shutil, "which", lambda _name: "powershell.exe"
    )
    monkeypatch.setattr(local_environment_module.subprocess, "run", run)

    assert local_environment_module._windows_acl_is_restricted(tmp_path / ".env")

    assert set(SECRET_ENVIRONMENT_KEYS).isdisjoint(captured)
    assert "PGPASSWORD" not in captured
    assert captured["AZURPILOT_ENV_ACL_PATH"] == str(tmp_path / ".env")
    assert {key: os.environ[key] for key in original_values} == original_values


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL gate")
def test_local_env_reports_unavailable_acl_inspection(tmp_path: Path, monkeypatch):
    path = tmp_path / ".env"
    _write_env(path, _document())
    monkeypatch.setattr(local_environment_module.shutil, "which", lambda _name: None)

    with pytest.raises(StorageConfigurationUnknownError, match="Не удалось подтвердить ACL"):
        load_local_postgres_environment(path, environment={})


def test_missing_local_env_rejects_broken_symlink_alias(tmp_path: Path, monkeypatch):
    path = tmp_path / ".env"
    original_lstat = Path.lstat

    def lstat(candidate: Path):
        if candidate == path:
            return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777)
        return original_lstat(candidate)

    monkeypatch.setattr(Path, "lstat", lstat)

    with pytest.raises(StorageConfigurationError, match="небезопасно"):
        load_local_postgres_environment(path, environment={})
