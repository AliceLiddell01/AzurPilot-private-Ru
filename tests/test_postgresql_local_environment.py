from __future__ import annotations

import os
from pathlib import Path

import pytest

from module.application.errors import StorageConfigurationError
from module.persistence.config import DatabaseSettings
from module.persistence.local_environment import load_local_postgres_environment


def _document() -> str:
    return "\n".join(
        [
            "AZURPILOT_POSTGRES_HOST=127.0.0.1",
            "AZURPILOT_POSTGRES_PORT=5432",
            "AZURPILOT_POSTGRES_DATABASE=azurpilot",
            "AZURPILOT_POSTGRES_USER=azurpilot_app",
            "AZURPILOT_POSTGRES_PASSWORD=app-secret",
            "AZURPILOT_POSTGRES_SSLMODE=disable",
            "AZURPILOT_POSTGRES_RUNTIME_TIMEZONE=Asia/Novosibirsk",
            "AZURPILOT_POSTGRES_PGPASSFILE=C:/secure/pgpass.conf",
            "AZURPILOT_POSTGRES_MIGRATOR_HOST=127.0.0.1",
            "AZURPILOT_POSTGRES_MIGRATOR_PORT=5432",
            "AZURPILOT_POSTGRES_MIGRATOR_DATABASE=azurpilot",
            "AZURPILOT_POSTGRES_MIGRATOR_USER=azurpilot_migrator",
            "AZURPILOT_POSTGRES_MIGRATOR_PASSWORD=migrator-secret",
            "AZURPILOT_POSTGRES_MIGRATOR_SSLMODE=disable",
            "AZURPILOT_POSTGRES_MIGRATOR_RUNTIME_TIMEZONE=Asia/Novosibirsk",
            "AZURPILOT_POSTGRES_MIGRATOR_PGPASSFILE=C:/secure/pgpass.conf",
            "AZURPILOT_WSL_DISTRO=archlinux",
            "AZURPILOT_WSL_PGPASSFILE=/etc/azurpilot/pgpass",
            "",
        ]
    )


def test_local_env_installs_metadata_and_passfile_without_secret_environment(
    tmp_path: Path,
):
    path = tmp_path / ".env"
    path.write_text(_document(), encoding="utf-8")
    environment = {"PGPASSWORD": "stale", "AZURPILOT_POSTGRES_PASSWORD": "stale"}

    local = load_local_postgres_environment(path, environment=environment)

    assert local is not None
    assert environment["AZURPILOT_POSTGRES_USER"] == "azurpilot_app"
    assert environment["PGPASSFILE"] == "C:/secure/pgpass.conf"
    assert "PGPASSWORD" not in environment
    assert "AZURPILOT_POSTGRES_PASSWORD" not in environment
    assert "AZURPILOT_POSTGRES_MIGRATOR_PASSWORD" not in environment


def test_local_env_can_select_migrator_without_exporting_secret(tmp_path: Path):
    path = tmp_path / ".env"
    path.write_text(_document(), encoding="utf-8")
    environment: dict[str, str] = {}

    load_local_postgres_environment(path, role="migrator", environment=environment)

    assert environment["AZURPILOT_POSTGRES_USER"] == "azurpilot_migrator"
    assert environment["PGPASSFILE"] == "C:/secure/pgpass.conf"
    assert all("secret" not in value for value in environment.values())


def test_local_env_rejects_unknown_or_duplicate_contract_key(tmp_path: Path):
    path = tmp_path / ".env"
    path.write_text(_document() + "AZURPILOT_POSTGRES_UNUSED=value\n", encoding="utf-8")
    with pytest.raises(StorageConfigurationError, match="Ключ"):
        load_local_postgres_environment(path, environment={})

    path.write_text(
        _document() + "AZURPILOT_POSTGRES_HOST=localhost\n", encoding="utf-8"
    )
    with pytest.raises(StorageConfigurationError, match="Ключ"):
        load_local_postgres_environment(path, environment={})


def test_local_env_requires_distinct_secrets_and_full_contract(tmp_path: Path):
    path = tmp_path / ".env"
    path.write_text(
        _document().replace("migrator-secret", "app-secret"), encoding="utf-8"
    )
    with pytest.raises(StorageConfigurationError, match="разные"):
        load_local_postgres_environment(path, environment={})

    path.write_text(
        _document().replace("AZURPILOT_POSTGRES_PORT=5432\n", "", 1),
        encoding="utf-8",
    )
    with pytest.raises(StorageConfigurationError, match="полный"):
        load_local_postgres_environment(path, environment={})


def test_local_env_runtime_contract_must_match_marker(tmp_path: Path):
    path = tmp_path / ".env"
    path.write_text(_document(), encoding="utf-8")
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
    local.require_runtime_match(settings)

    with pytest.raises(StorageConfigurationError, match="не совпадает"):
        local.require_runtime_match(
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
