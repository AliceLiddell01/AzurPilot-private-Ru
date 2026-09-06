from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from module.application.errors import StorageConfigurationError
from module.persistence import local_environment as local_environment_module
from module.persistence.config import DatabaseSettings
from module.persistence.local_environment import (
    LocalPostgresEnvironment,
    load_local_postgres_environment,
)


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


def test_local_env_rejects_bare_docker_namespace_key(tmp_path: Path):
    path = tmp_path / ".env"
    _write_env(path, _document() + "AZURPILOT_POSTGRES_DOCKER_=value\n")

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
    with pytest.raises(StorageConfigurationError, match="полный"):
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
    with pytest.raises(StorageConfigurationError, match="полный"):
        LocalPostgresEnvironment(path=tmp_path / ".env", values={})


def test_direct_local_environment_rejects_extra_contract_key(tmp_path: Path):
    values = dict(line.split("=", 1) for line in _document().splitlines() if line)
    values["UNEXPECTED"] = "value"
    with pytest.raises(StorageConfigurationError, match="полный"):
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
    with pytest.raises(StorageConfigurationError, match="endpoints"):
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


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL gate")
def test_local_env_reports_unavailable_acl_inspection(tmp_path: Path, monkeypatch):
    path = tmp_path / ".env"
    _write_env(path, _document())
    monkeypatch.setattr(local_environment_module.shutil, "which", lambda _name: None)

    with pytest.raises(StorageConfigurationError, match="PowerShell"):
        load_local_postgres_environment(path, environment={})


def test_missing_local_env_rejects_broken_symlink_alias(tmp_path: Path, monkeypatch):
    path = tmp_path / ".env"
    original_exists = Path.exists
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "exists",
        lambda candidate: False if candidate == path else original_exists(candidate),
    )
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda candidate: True if candidate == path else original_is_symlink(candidate),
    )

    with pytest.raises(StorageConfigurationError, match="небезопасен"):
        load_local_postgres_environment(path, environment={})
