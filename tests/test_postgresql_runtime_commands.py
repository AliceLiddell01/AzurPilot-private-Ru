from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy.exc import OperationalError

from dev_tools import postgresql_runtime
from module.persistence.config import DatabaseSettings


def _settings(password: str | None = None) -> DatabaseSettings:
    return DatabaseSettings(
        host="127.0.0.1",
        port=5432,
        database="azurpilot",
        user="azurpilot_app",
        password=password,
        sslmode="disable",
    )


def test_backup_rejects_repository_target(tmp_path: Path):
    repository = tmp_path / "repository"
    repository.mkdir()

    with pytest.raises(RuntimeError, match="вне репозитория"):
        postgresql_runtime._backup(
            _settings(),
            repository / "backup.dump",
            "Archlinux",
            repository,
        )


def test_backup_is_verified_and_published_create_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repository = tmp_path / "repository"
    repository.mkdir()
    output = tmp_path / "backups" / "production.dump"
    calls: list[tuple[list[str], dict[str, str] | None]] = []
    monkeypatch.setenv("AZURPILOT_POSTGRES_PGPASSFILE", "C:/secure/pgpass.conf")

    def run_hidden(
        arguments: list[str],
        *,
        stdout: object = None,
        environment: dict[str, str] | None = None,
    ) -> None:
        calls.append((arguments, environment))
        if hasattr(stdout, "write"):
            stdout.write(b"x" * 2048)

    with (
        patch.object(postgresql_runtime.shutil, "which", side_effect=["pg_dump", "pg_restore"]),
        patch.object(postgresql_runtime, "_run_hidden", side_effect=run_hidden),
    ):
        postgresql_runtime._backup(
            _settings("test-password"), output, "Archlinux", repository
        )

    assert output.stat().st_size == 2048
    assert calls[0][0][0] == "pg_dump"
    assert "PGPASSWORD" not in calls[0][1]
    assert calls[0][1]["PGPASSFILE"] == "C:/secure/pgpass.conf"
    assert calls[1][0][:2] == ["pg_restore", "--list"]
    assert not tuple(output.parent.glob("*.tmp"))

    with pytest.raises(RuntimeError, match="уже существует"):
        postgresql_runtime._backup(
            _settings(), output, "Archlinux", repository
        )


def test_upgrade_removes_application_password_for_passwordless_migrator(monkeypatch):
    monkeypatch.setenv("AZURPILOT_POSTGRES_PASSWORD", "stale-application-password")
    settings = _settings(password=None)

    with (
        patch.object(
            postgresql_runtime.DatabaseSettings,
            "from_environment",
            return_value=settings,
        ),
        patch.object(postgresql_runtime.command, "upgrade") as upgrade,
    ):
        postgresql_runtime._upgrade()

    assert "AZURPILOT_POSTGRES_PASSWORD" not in os.environ
    upgrade.assert_called_once()


def test_runtime_command_redacts_sqlalchemy_diagnostics(capsys):
    diagnostic = OperationalError(
        "SELECT secret_value",
        {},
        RuntimeError("password=secret-value"),
    )
    parser = SimpleNamespace(
        parse_args=lambda _argv: SimpleNamespace(command="upgrade")
    )

    with (
        patch.object(postgresql_runtime, "_parser", return_value=parser),
        patch.object(postgresql_runtime, "_upgrade", side_effect=diagnostic),
    ):
        assert postgresql_runtime.main([]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "Ошибка production PostgreSQL: операция с базой данных завершилась ошибкой.\n"
    )
    assert "secret" not in captured.err


def test_default_marker_resolution_runs_legacy_migration():
    with patch.object(postgresql_runtime, "migrate_legacy_backend_marker") as migrate:
        marker = postgresql_runtime._resolve_marker(
            postgresql_runtime.DEFAULT_BACKEND_MARKER_PATH
        )

    assert marker == postgresql_runtime.DEFAULT_BACKEND_MARKER_PATH
    migrate.assert_called_once_with()
