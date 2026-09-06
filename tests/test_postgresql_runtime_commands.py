from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy.exc import OperationalError

from dev_tools import postgresql_runtime
from module.application.errors import StorageConfigurationError
from module.application.storage_models import StorageHealth, StorageHealthState
from module.persistence.config import DatabaseSettings
from module.persistence.schema import EXPECTED_ALEMBIC_HEAD


def _settings(
    password: str | None = None,
    *,
    user: str = "azurpilot_app",
) -> DatabaseSettings:
    return DatabaseSettings(
        host="127.0.0.1",
        port=5432,
        database="azurpilot",
        user=user,
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
        patch.object(
            postgresql_runtime.DatabaseSettings,
            "from_environment",
            return_value=_settings("migrator-password", user="azurpilot_migrator"),
        ),
        patch.object(postgresql_runtime, "_run_hidden", side_effect=run_hidden),
    ):
        postgresql_runtime._backup(
            _settings("test-password"),
            output,
            "Archlinux",
            repository,
            transport="native",
        )

    assert output.stat().st_size == 2048
    assert calls[0][0][0] == "pg_dump"
    assert "azurpilot_migrator" in calls[0][0]
    assert "PGPASSWORD" not in calls[0][1]
    assert calls[0][1]["PGPASSFILE"] == "C:/secure/pgpass.conf"
    assert calls[1][0][:2] == ["pg_restore", "--list"]
    assert calls[1][1] is not None
    assert "PGPASSWORD" not in calls[1][1]
    assert not tuple(output.parent.glob("*.tmp"))

    with pytest.raises(RuntimeError, match="уже существует"):
        postgresql_runtime._backup(
            _settings(), output, "Archlinux", repository, transport="native"
        )


def test_wsl_backup_formats_rollback_restore_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repository = tmp_path / "repository"
    repository.mkdir()
    output = tmp_path / "backups" / "rollback.dump"
    calls: list[tuple[list[str], dict[str, str] | None]] = []
    monkeypatch.setenv("AZURPILOT_WSL_PGPASSFILE", "/etc/azurpilot/pgpass")

    def run_hidden(
        arguments: list[str],
        *,
        stdin: object = None,
        stdout: object = None,
        environment: dict[str, str] | None = None,
    ) -> None:
        del stdin
        calls.append((arguments, environment))
        if hasattr(stdout, "write"):
            stdout.write(b"x" * 2048)

    with (
        patch.object(
            postgresql_runtime.DatabaseSettings,
            "from_environment",
            return_value=_settings("migrator-password", user="azurpilot_migrator"),
        ),
        patch.object(postgresql_runtime, "_run_hidden", side_effect=run_hidden),
    ):
        postgresql_runtime._backup(
            _settings("test-password"),
            output,
            "Archlinux",
            repository,
            transport="wsl",
        )

    assert output.stat().st_size == 2048
    assert calls[0][0][0:4] == ["wsl.exe", "--distribution", "Archlinux", "--exec"]
    assert calls[1][0][0:4] == ["wsl.exe", "--distribution", "Archlinux", "--exec"]
    assert calls[0][0][calls[0][0].index("--username") + 1] == "azurpilot_migrator"
    assert calls[1][0][-2] == "--list"
    assert calls[1][0][-1].startswith("/mnt/")
    assert "temporary-wsl" not in calls[1][0][-1]
    assert calls[0][1] is not None
    assert calls[1][1] is calls[0][1]
    assert "PGPASSWORD" not in calls[0][1]


def test_native_backup_does_not_fall_back_to_wsl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repository = tmp_path / "repository"
    repository.mkdir()
    output = tmp_path / "backups" / "native.dump"
    with (
        patch.object(
            postgresql_runtime.DatabaseSettings,
            "from_environment",
            return_value=_settings("migrator-password", user="azurpilot_migrator"),
        ),
        patch.object(postgresql_runtime.shutil, "which", return_value=None),
        pytest.raises(RuntimeError, match="Native pg_dump"),
    ):
        postgresql_runtime._backup(
            _settings("test-password"),
            output,
            "Archlinux",
            repository,
            transport="native",
        )


def test_docker_backup_requires_marker_endpoint(tmp_path: Path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    monkeypatch.setattr(
        postgresql_runtime,
        "_compose_arguments",
        lambda *_arguments: ["docker", "compose", "port"],
    )
    monkeypatch.setattr(
        postgresql_runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="127.0.0.1:5432\n", stderr=""
        ),
    )

    postgresql_runtime._require_docker_endpoint(_settings(), repository)

    monkeypatch.setattr(
        postgresql_runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="127.0.0.1:6543\n", stderr=""
        ),
    )
    with pytest.raises(StorageConfigurationError, match="endpoint"):
        postgresql_runtime._require_docker_endpoint(_settings(), repository)


def test_upgrade_removes_application_password_for_passwordless_migrator(monkeypatch):
    for key, value in {
        "AZURPILOT_POSTGRES_HOST": "test-original-host",
        "AZURPILOT_POSTGRES_PORT": "6543",
        "AZURPILOT_POSTGRES_DATABASE": "test_original_database",
        "AZURPILOT_POSTGRES_USER": "test_original_user",
        "AZURPILOT_POSTGRES_SSLMODE": "require",
        "AZURPILOT_POSTGRES_RUNTIME_TIMEZONE": "UTC",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("AZURPILOT_POSTGRES_PASSWORD", "stale-application-password")
    settings = _settings(password=None, user="azurpilot_migrator")
    ready = StorageHealth(StorageHealthState.READY, EXPECTED_ALEMBIC_HEAD)

    with (
        patch.object(
            postgresql_runtime,
            "load_local_postgres_environment",
            return_value=None,
        ),
        patch.object(
            postgresql_runtime,
            "load_backend_marker_for_schema_upgrade",
            return_value=(_settings(), EXPECTED_ALEMBIC_HEAD),
        ),
        patch.object(
            postgresql_runtime.DatabaseSettings,
            "from_environment",
            return_value=settings,
        ),
        patch.object(postgresql_runtime.StorageHealthChecker, "check", return_value=ready),
        patch.object(postgresql_runtime.StorageHealthChecker, "require_ready"),
        patch.object(postgresql_runtime.command, "upgrade") as upgrade,
        patch.object(postgresql_runtime, "advance_backend_marker_schema_head") as advance,
    ):
        postgresql_runtime._upgrade()

    assert "AZURPILOT_POSTGRES_PASSWORD" not in os.environ
    upgrade.assert_called_once()
    advance.assert_called_once_with(
        postgresql_runtime._REPOSITORY_ROOT
        / postgresql_runtime.DEFAULT_BACKEND_MARKER_PATH,
        previous_head=EXPECTED_ALEMBIC_HEAD,
    )


def test_prepare_current_marker_checks_app_health_without_upgrade(tmp_path: Path) -> None:
    marker = tmp_path / "storage_backend.json"
    events: list[object] = []

    class _Local:
        def require_app_runtime_match(self, settings: DatabaseSettings) -> None:
            events.append(("match", settings.user))

    with (
        patch.object(
            postgresql_runtime,
            "load_local_postgres_environment",
            return_value=_Local(),
        ),
        patch.object(
            postgresql_runtime,
            "load_backend_marker_for_schema_upgrade",
            return_value=(_settings(), EXPECTED_ALEMBIC_HEAD),
        ),
        patch.object(
            postgresql_runtime,
            "_require_upgrade_marker_revision",
            side_effect=lambda _configuration, head: events.append(("graph", head)),
        ),
        patch.object(
            postgresql_runtime,
            "_run_schema_upgrade_process",
            side_effect=lambda _marker: events.append("upgrade"),
        ),
        patch.object(
            postgresql_runtime,
            "_health",
            side_effect=lambda actual: events.append(("health", actual)),
        ),
    ):
        postgresql_runtime._prepare(marker)

    assert events == [
        ("match", "azurpilot_app"),
        ("graph", EXPECTED_ALEMBIC_HEAD),
        ("health", marker),
    ]


def test_prepare_stale_marker_upgrades_in_child_before_app_health(tmp_path: Path) -> None:
    marker = tmp_path / "storage_backend.json"
    previous_head = "0003_fleet_state_core"
    events: list[object] = []

    with (
        patch.object(
            postgresql_runtime,
            "load_local_postgres_environment",
            return_value=None,
        ),
        patch.object(
            postgresql_runtime,
            "load_backend_marker_for_schema_upgrade",
            return_value=(_settings(), previous_head),
        ),
        patch.object(
            postgresql_runtime,
            "_require_upgrade_marker_revision",
            side_effect=lambda _configuration, head: events.append(("graph", head)),
        ),
        patch.object(
            postgresql_runtime,
            "_run_schema_upgrade_process",
            side_effect=lambda actual: events.append(("upgrade", actual)),
        ),
        patch.object(
            postgresql_runtime,
            "_health",
            side_effect=lambda actual: events.append(("health", actual)),
        ),
    ):
        postgresql_runtime._prepare(marker)

    assert events == [
        ("graph", previous_head),
        ("upgrade", marker),
        ("health", marker),
    ]


def test_prepare_fails_closed_before_upgrade_for_invalid_marker_revision(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "storage_backend.json"

    with (
        patch.object(
            postgresql_runtime,
            "load_local_postgres_environment",
            return_value=None,
        ),
        patch.object(
            postgresql_runtime,
            "load_backend_marker_for_schema_upgrade",
            return_value=(_settings(), "unknown_revision"),
        ),
        patch.object(
            postgresql_runtime,
            "_require_upgrade_marker_revision",
            side_effect=StorageConfigurationError("invalid graph"),
        ),
        patch.object(postgresql_runtime, "_run_schema_upgrade_process") as run_upgrade,
        patch.object(postgresql_runtime, "_health") as app_health,
        pytest.raises(StorageConfigurationError, match="invalid graph"),
    ):
        postgresql_runtime._prepare(marker)

    run_upgrade.assert_not_called()
    app_health.assert_not_called()


def test_schema_upgrade_process_uses_isolated_python_child(tmp_path: Path) -> None:
    marker = tmp_path / "storage_backend.json"
    completed = SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch.object(
        postgresql_runtime.subprocess,
        "run",
        return_value=completed,
    ) as run:
        postgresql_runtime._run_schema_upgrade_process(marker)

    arguments = run.call_args.args[0]
    options = run.call_args.kwargs
    assert arguments == [
        postgresql_runtime.sys.executable,
        "-X",
        "utf8",
        "-m",
        "dev_tools.postgresql_runtime",
        "upgrade",
        "--marker",
        str(marker),
    ]
    assert options["cwd"] == postgresql_runtime._REPOSITORY_ROOT
    assert options["timeout"] == 180
    assert options["env"]["PYTHONUTF8"] == "1"
    assert options["check"] is False


def test_start_preflight_uses_prepare_for_schema_reconciliation() -> None:
    script = (
        postgresql_runtime._REPOSITORY_ROOT / "scripts" / "Start-AzurPilot.ps1"
    ).read_text(encoding="utf-8-sig")

    assert (
        "Arguments = @('-X', 'utf8', '-m', 'dev_tools.postgresql_runtime', 'prepare')"
        in script
    )
    assert "TimeoutMilliseconds = 210000" in script
    assert (
        "Production PostgreSQL не прошёл подготовку marker, schema upgrade или app-health."
        in script
    )


def test_runtime_command_redacts_sqlalchemy_diagnostics(capsys):
    diagnostic = OperationalError(
        "SELECT secret_value",
        {},
        RuntimeError("password=secret-value"),
    )
    parser = SimpleNamespace(
        parse_args=lambda _argv: SimpleNamespace(
            command="upgrade",
            marker=str(postgresql_runtime.DEFAULT_BACKEND_MARKER_PATH),
        )
    )

    with (
        patch.object(postgresql_runtime, "_parser", return_value=parser),
        patch.object(postgresql_runtime, "_resolve_marker", return_value=Path("marker")),
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

    assert marker == (
        postgresql_runtime._REPOSITORY_ROOT
        / postgresql_runtime.DEFAULT_BACKEND_MARKER_PATH
    )
    migrate.assert_called_once_with(
        target=marker,
        legacy=postgresql_runtime._REPOSITORY_ROOT / "config/storage_backend.json",
    )
