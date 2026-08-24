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
from alembic.util.exc import CommandError
from sqlalchemy.exc import SQLAlchemyError

from module.application.errors import StorageError
from module.persistence.config import DEFAULT_BACKEND_MARKER_PATH, DatabaseSettings
from module.persistence.database import LazyEngine, StorageHealthChecker
from module.persistence.local_environment import load_local_postgres_environment


def _run_hidden(
    arguments: list[str],
    *,
    stdout: object = subprocess.DEVNULL,
    environment: dict[str, str] | None = None,
) -> None:
    options: dict[str, object] = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    result = subprocess.run(
        arguments,
        env=environment,
        stdin=subprocess.DEVNULL,
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
) -> None:
    output = _validate_external_output(output, repository_root)
    native = shutil.which("pg_dump")
    environment = os.environ.copy()
    environment.pop("PGPASSWORD", None)
    if native:
        environment["PGPASSFILE"] = os.environ.get(
            "AZURPILOT_POSTGRES_PGPASSFILE", environment.get("PGPASSFILE", "")
        )
    arguments = (
        [native, *_pg_dump_arguments(settings)]
        if native
        else [
            "wsl.exe",
            "--distribution",
            distro,
            "--exec",
            "env",
            f"PGPASSFILE={os.environ.get('AZURPILOT_WSL_PGPASSFILE', '/etc/azurpilot/pgpass')}",
            "pg_dump",
            *_pg_dump_arguments(settings),
        ]
    )
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
        restore = shutil.which("pg_restore")
        restore_arguments = (
            [restore, "--list", str(temporary)]
            if restore
            else [
                "wsl.exe",
                "--distribution",
                distro,
                "--exec",
                "pg_restore",
                "--list",
                _wsl_path(temporary),
            ]
        )
        _run_hidden(restore_arguments)
        os.link(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def _health(marker: Path) -> None:
    settings = DatabaseSettings.from_backend_marker(marker)
    engine = LazyEngine(settings)
    try:
        StorageHealthChecker(engine).require_ready()
    finally:
        engine.dispose()


def _upgrade() -> None:
    load_local_postgres_environment(role="migrator")
    settings = DatabaseSettings.from_environment(
        prefix="AZURPILOT_POSTGRES_MIGRATOR_"
    )
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
    configuration = Config("alembic.ini")
    command.upgrade(configuration, "head")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Эксплуатационные команды production PostgreSQL AzurPilot."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    health = subparsers.add_parser("health", help="Проверить marker, доступ и schema head.")
    health.add_argument("--marker", default=str(DEFAULT_BACKEND_MARKER_PATH))

    backup = subparsers.add_parser("backup", help="Создать проверяемый custom dump.")
    backup.add_argument("--marker", default=str(DEFAULT_BACKEND_MARKER_PATH))
    backup.add_argument("--output", required=True)
    backup.add_argument("--distro", default="Archlinux")
    backup.add_argument("--repository-root", default=".")

    subparsers.add_parser("upgrade", help="Применить Alembic от имени migrator.")

    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command in {"health", "backup"}:
            load_local_postgres_environment(role="app")
        if arguments.command == "health":
            _health(Path(arguments.marker))
        elif arguments.command == "backup":
            settings = DatabaseSettings.from_backend_marker(arguments.marker)
            _backup(
                settings,
                Path(arguments.output),
                arguments.distro,
                Path(arguments.repository_root),
            )
        elif arguments.command == "upgrade":
            _upgrade()
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
