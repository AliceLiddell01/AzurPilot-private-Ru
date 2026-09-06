"""Fail-closed эксплуатационные команды production PostgreSQL."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from sqlalchemy.exc import SQLAlchemyError

from module.application.errors import StorageConfigurationError, StorageError
from module.application.storage_models import StorageHealthState
from module.persistence.config import (
    DEFAULT_BACKEND_MARKER_PATH,
    DatabaseSettings,
    advance_backend_marker_schema_head,
    load_backend_marker_for_schema_upgrade,
    migrate_legacy_backend_marker,
)
from module.persistence.database import LazyEngine, StorageHealthChecker
from module.persistence.local_environment import load_local_postgres_environment
from module.persistence.schema import EXPECTED_ALEMBIC_HEAD

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _run_hidden(
    arguments: list[str],
    *,
    stdin: object = subprocess.DEVNULL,
    stdout: object = subprocess.DEVNULL,
    environment: dict[str, str] | None = None,
) -> None:
    options: dict[str, object] = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    result = subprocess.run(
        arguments,
        env=environment,
        stdin=stdin,
        stdout=stdout,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=180,
        **options,
    )
    if result.returncode != 0:
        raise RuntimeError("Эксплуатационная команда PostgreSQL завершилась ошибкой.")


def _pg_dump_arguments(settings: DatabaseSettings) -> list[str]:
    return [
        "--host",
        settings.host,
        "--port",
        str(settings.port),
        "--username",
        settings.user,
        "--format=custom",
        "--no-owner",
        "--no-acl",
        settings.database,
    ]


def _wsl_path(path: Path) -> str:
    drive = path.drive.rstrip(":").lower()
    if not drive:
        raise RuntimeError("Путь Windows для WSL не содержит букву диска.")
    suffix = path.as_posix().split(":", 1)[1]
    return f"/mnt/{drive}{suffix}"


def _docker_executable() -> str:
    executable = shutil.which("docker.exe") or shutil.which("docker")
    if executable is None:
        raise RuntimeError("Docker CLI недоступен для PostgreSQL backup.")
    return executable


def _compose_arguments(repository_root: Path, *arguments: str) -> list[str]:
    repository_root = repository_root.resolve(strict=True)
    env_file = repository_root / ".env"
    compose_file = repository_root / "infrastructure/observability/compose.yaml"
    if not env_file.is_file() or not compose_file.is_file():
        raise RuntimeError("Канонический Docker Compose PostgreSQL недоступен.")
    return [
        _docker_executable(),
        "compose",
        "--env-file",
        str(env_file),
        "--file",
        str(compose_file),
        *arguments,
    ]


def _maintenance_settings(marker_settings: DatabaseSettings) -> DatabaseSettings:
    settings = DatabaseSettings.from_environment(
        prefix="AZURPILOT_POSTGRES_MIGRATOR_"
    )
    _require_upgrade_endpoint_match(marker_settings, settings)
    return settings


def _validate_external_output(output: Path, repository_root: Path) -> Path:
    output = output.resolve()
    repository_root = repository_root.resolve(strict=True)
    try:
        output.relative_to(repository_root)
    except ValueError:
        pass
    else:
        raise RuntimeError("Резервная копия PostgreSQL должна находиться вне репозитория.")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise RuntimeError("Файл резервной копии уже существует.")
    return output


def _backup(
    settings: DatabaseSettings,
    output: Path,
    distro: str,
    repository_root: Path,
    *,
    transport: str = "docker",
) -> None:
    output = _validate_external_output(output, repository_root)
    environment = os.environ.copy()
    environment.pop("PGPASSWORD", None)
    environment.pop("AZURPILOT_POSTGRES_PASSWORD", None)
    environment.pop("AZURPILOT_POSTGRES_MIGRATOR_PASSWORD", None)

    if transport == "docker":
        arguments = _compose_arguments(
            repository_root,
            "exec",
            "-T",
            "--user",
            "postgres",
            "postgres",
            "pg_dump",
            "--username",
            "postgres",
            "--format=custom",
            "--no-owner",
            "--no-acl",
            settings.database,
        )
        restore_arguments = _compose_arguments(
            repository_root,
            "exec",
            "-T",
            "--user",
            "postgres",
            "postgres",
            "pg_restore",
            "--list",
        )
    elif transport in {"native", "wsl"}:
        maintenance = _maintenance_settings(settings)
        native = shutil.which("pg_dump") if transport == "native" else None
        if native:
            passfile = environment.get(
                "AZURPILOT_POSTGRES_MIGRATOR_PGPASSFILE"
            ) or environment.get("AZURPILOT_POSTGRES_PGPASSFILE")
            if passfile:
                environment["PGPASSFILE"] = passfile
            arguments = [native, *_pg_dump_arguments(maintenance)]
        else:
            arguments = [
                "wsl.exe",
                "--distribution",
                distro,
                "--exec",
                "env",
                f"PGPASSFILE={os.environ.get('AZURPILOT_WSL_PGPASSFILE', '/etc/azurpilot/pgpass')}",
                "pg_dump",
                *_pg_dump_arguments(maintenance),
            ]
        restore = shutil.which("pg_restore") if native else None
        restore_arguments = (
            [restore, "--list", "{temporary}"]
            if restore
            else [
                "wsl.exe",
                "--distribution",
                distro,
                "--exec",
                "pg_restore",
                "--list",
                "{temporary-wsl}",
            ]
        )
    else:
        raise ValueError("Транспорт PostgreSQL backup не поддерживается.")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=output.name + ".", suffix=".tmp", dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("wb") as stream:
            _run_hidden(arguments, stdout=stream, environment=environment)
        if temporary.stat().st_size < 1024:
            raise RuntimeError("Резервная копия PostgreSQL неожиданно мала.")
        if transport == "docker":
            with temporary.open("rb") as stream:
                _run_hidden(restore_arguments, stdin=stream)
        else:
            restore_arguments = [
                argument.format(
                    temporary=str(temporary),
                    temporary_wsl=_wsl_path(temporary),
                )
                for argument in restore_arguments
            ]
            _run_hidden(restore_arguments)
        os.link(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def _resolve_marker(value: str | Path) -> Path:
    marker = Path(value)
    if marker == DEFAULT_BACKEND_MARKER_PATH:
        marker = _REPOSITORY_ROOT / marker
        migrate_legacy_backend_marker(
            target=marker,
            legacy=_REPOSITORY_ROOT / "config/storage_backend.json",
        )
    return marker


def _health(marker: Path) -> None:
    settings = DatabaseSettings.from_backend_marker(marker)
    engine = LazyEngine(settings)
    try:
        StorageHealthChecker(engine).require_ready()
    finally:
        engine.dispose()


def _require_upgrade_endpoint_match(
    marker_settings: DatabaseSettings,
    migrator_settings: DatabaseSettings,
) -> None:
    """Разрешить только штатную migrator-роль на production marker endpoint."""

    if migrator_settings.user != "azurpilot_migrator":
        raise StorageConfigurationError(
            "Production schema upgrade требует роль azurpilot_migrator."
        )
    if (
        marker_settings.host != migrator_settings.host
        or marker_settings.port != migrator_settings.port
        or marker_settings.database != migrator_settings.database
        or marker_settings.sslmode != migrator_settings.sslmode
        or marker_settings.runtime_timezone != migrator_settings.runtime_timezone
    ):
        raise StorageConfigurationError(
            "Migrator endpoint не совпадает с production backend marker."
        )


def _require_upgrade_marker_revision(
    configuration: Config,
    marker_head: str,
) -> None:
    """Разрешить только известную ревизию-предка текущего Alembic head."""

    scripts = ScriptDirectory.from_config(configuration)
    if set(scripts.get_heads()) != {EXPECTED_ALEMBIC_HEAD}:
        raise StorageConfigurationError(
            "Alembic graph не соответствует ожидаемому production schema head."
        )
    allowed_revisions = {
        script.revision
        for script in scripts.iterate_revisions(EXPECTED_ALEMBIC_HEAD, "base")
        if script.revision is not None
    }
    allowed_revisions.add(EXPECTED_ALEMBIC_HEAD)
    if marker_head not in allowed_revisions:
        raise StorageConfigurationError(
            "Production backend marker содержит неизвестный или недопустимый schema head."
        )


def _upgrade(
    marker: Path = _REPOSITORY_ROOT / DEFAULT_BACKEND_MARKER_PATH,
) -> None:
    local = load_local_postgres_environment(
        _REPOSITORY_ROOT / ".env",
        role="migrator",
    )
    marker_settings, marker_head = load_backend_marker_for_schema_upgrade(marker)
    if local is not None:
        local.require_app_runtime_match(marker_settings)

    settings = DatabaseSettings.from_environment(
        prefix="AZURPILOT_POSTGRES_MIGRATOR_"
    )
    _require_upgrade_endpoint_match(marker_settings, settings)
    configuration = Config(str(_REPOSITORY_ROOT / "alembic.ini"))
    _require_upgrade_marker_revision(configuration, marker_head)
    os.environ.update(
        {
            "AZURPILOT_POSTGRES_HOST": settings.host,
            "AZURPILOT_POSTGRES_PORT": str(settings.port),
            "AZURPILOT_POSTGRES_DATABASE": settings.database,
            "AZURPILOT_POSTGRES_USER": settings.user,
            "AZURPILOT_POSTGRES_SSLMODE": settings.sslmode,
            "AZURPILOT_POSTGRES_RUNTIME_TIMEZONE": settings.runtime_timezone,
        }
    )
    os.environ.pop("AZURPILOT_POSTGRES_PASSWORD", None)
    os.environ.pop("PGPASSWORD", None)

    engine = LazyEngine(settings)
    try:
        previous_health = StorageHealthChecker(
            engine,
            expected_head=marker_head,
        ).check()
        current_health = StorageHealthChecker(engine).check()
        if previous_health.state is StorageHealthState.READY:
            command.upgrade(configuration, "head")
        elif current_health.state is not StorageHealthState.READY:
            StorageHealthChecker(
                engine,
                expected_head=marker_head,
            ).require_ready()
        StorageHealthChecker(engine).require_ready()
    finally:
        engine.dispose()

    advance_backend_marker_schema_head(
        marker,
        previous_head=marker_head,
    )


def _run_schema_upgrade_process(marker: Path) -> None:
    """Выполнить migrator upgrade в отдельном процессе, не меняя app environment."""

    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    arguments = [
        sys.executable,
        "-X",
        "utf8",
        "-m",
        "dev_tools.postgresql_runtime",
        "upgrade",
        "--marker",
        str(marker),
    ]
    options: dict[str, object] = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(
            arguments,
            cwd=_REPOSITORY_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=180,
            text=True,
            encoding="utf-8",
            errors="replace",
            **options,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "Штатный schema upgrade PostgreSQL превысил 180 секунд."
        ) from exc
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        if details.startswith("Ошибка production PostgreSQL:"):
            details = details.removeprefix("Ошибка production PostgreSQL:").strip()
        if not details:
            details = f"код завершения {result.returncode}"
        raise RuntimeError(f"Штатный schema upgrade PostgreSQL не выполнен: {details}")


def _prepare(
    marker: Path = _REPOSITORY_ROOT / DEFAULT_BACKEND_MARKER_PATH,
) -> None:
    """Подготовить production PostgreSQL к запуску без смешивания app/migrator ролей."""

    local = load_local_postgres_environment(
        _REPOSITORY_ROOT / ".env",
        role="app",
    )
    marker_settings, marker_head = load_backend_marker_for_schema_upgrade(marker)
    if local is not None:
        local.require_app_runtime_match(marker_settings)

    configuration = Config(str(_REPOSITORY_ROOT / "alembic.ini"))
    _require_upgrade_marker_revision(configuration, marker_head)
    if marker_head != EXPECTED_ALEMBIC_HEAD:
        _run_schema_upgrade_process(marker)

    _health(marker)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Эксплуатационные команды production PostgreSQL AzurPilot."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    health = subparsers.add_parser("health", help="Проверить marker, доступ и schema head.")
    health.add_argument("--marker", default=str(DEFAULT_BACKEND_MARKER_PATH))

    prepare = subparsers.add_parser(
        "prepare",
        help="Подготовить schema и проверить app-доступ перед запуском.",
    )
    prepare.add_argument("--marker", default=str(DEFAULT_BACKEND_MARKER_PATH))

    backup = subparsers.add_parser("backup", help="Создать проверяемый custom dump.")
    backup.add_argument("--marker", default=str(DEFAULT_BACKEND_MARKER_PATH))
    backup.add_argument("--output", required=True)
    backup.add_argument("--distro", default="Archlinux")
    backup.add_argument(
        "--transport",
        choices=("docker", "native", "wsl"),
        default="docker",
        help="Источник pg_dump: Docker Compose по умолчанию или rollback-транспорт.",
    )
    backup.add_argument("--repository-root", default=".")

    upgrade = subparsers.add_parser(
        "upgrade",
        help="Применить Alembic от имени migrator.",
    )
    upgrade.add_argument("--marker", default=str(DEFAULT_BACKEND_MARKER_PATH))

    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command in {"health", "backup"}:
            load_local_postgres_environment(_REPOSITORY_ROOT / ".env", role="app")
        if arguments.command == "health":
            _health(_resolve_marker(arguments.marker))
        elif arguments.command == "prepare":
            _prepare(_resolve_marker(arguments.marker))
        elif arguments.command == "backup":
            settings = DatabaseSettings.from_backend_marker(
                _resolve_marker(arguments.marker)
            )
            _backup(
                settings,
                Path(arguments.output),
                arguments.distro,
                Path(arguments.repository_root),
                transport=arguments.transport,
            )
        elif arguments.command == "upgrade":
            _upgrade(_resolve_marker(arguments.marker))
        else:
            raise RuntimeError("Неизвестная эксплуатационная команда.")
    except (CommandError, SQLAlchemyError):
        print(
            "Ошибка production PostgreSQL: операция с базой данных завершилась ошибкой.",
            file=sys.stderr,
        )
        return 1
    except (OSError, RuntimeError, StorageError, ValueError) as exc:
        print(f"Ошибка production PostgreSQL: {exc}", file=sys.stderr)
        return 1
    print("Операция production PostgreSQL завершена успешно.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
