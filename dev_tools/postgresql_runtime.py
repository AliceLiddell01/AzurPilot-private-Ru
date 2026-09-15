"""Эксплуатационные команды PostgreSQL с fail-closed поведением."""

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

from azurpilot.tooling.filesystem import is_unsafe_path, path_has_link
from azurpilot.tooling.process import DOCKER_ENVIRONMENT_KEYS
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
from tools.paths import REPOSITORY_ROOT

_REPOSITORY_ROOT = REPOSITORY_ROOT


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


def _backup_process_environment(
    *,
    passfile: str | None = None,
    sslmode: str | None = None,
    sslrootcert: str | None = None,
) -> dict[str, str]:
    """Передать backup-процессу только системные и явно подтверждённые переменные."""

    allowed = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "ProgramData",
        "ProgramFiles",
        "ProgramFiles(x86)",
        "CommonProgramFiles",
        "CommonProgramFiles(x86)",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "COMMONPROGRAMFILES",
        "COMMONPROGRAMFILES(X86)",
        "TEMP",
        "TMP",
        "TMPDIR",
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
    } | DOCKER_ENVIRONMENT_KEYS
    environment = {
        key: value for key, value in os.environ.items() if key in allowed
    }
    if passfile:
        environment["PGPASSFILE"] = passfile
    if sslmode:
        environment["PGSSLMODE"] = sslmode
    if sslrootcert:
        environment["PGSSLROOTCERT"] = sslrootcert
    return environment


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
        raise RuntimeError("Docker CLI недоступен для резервного копирования PostgreSQL.")
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


def _require_docker_endpoint(
    marker_settings: DatabaseSettings,
    repository_root: Path,
) -> None:
    if marker_settings.host not in {"127.0.0.1", "localhost", "::1"}:
        raise StorageConfigurationError(
            "Конечная точка PostgreSQL из маркера Docker не ограничена loopback."
        )
    options: dict[str, object] = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    result = subprocess.run(
        _compose_arguments(repository_root, "port", "postgres", "5432"),
        env=_backup_process_environment(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
        **options,
    )
    if result.returncode != 0:
        raise RuntimeError("Конечная точка PostgreSQL Docker Compose недоступна.")
    bindings = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not bindings:
        raise RuntimeError("Конечная точка PostgreSQL Docker Compose не опубликована.")
    for binding in bindings:
        host_port = binding.rsplit(":", 1)
        if len(host_port) != 2:
            raise StorageConfigurationError(
                "Конечная точка PostgreSQL Docker не совпадает с рабочим маркером."
            )
        host = host_port[0].strip("[]")
        try:
            port = int(host_port[1])
        except ValueError as exc:
            raise StorageConfigurationError(
                "Конечная точка PostgreSQL Docker содержит некорректный порт."
            ) from exc
        if host not in {"127.0.0.1", "localhost", "::1"} or port != marker_settings.port:
            raise StorageConfigurationError(
                "Конечная точка PostgreSQL Docker не совпадает с рабочим маркером."
            )


def _validate_external_output(output: Path, repository_root: Path) -> Path:
    output = Path(output).expanduser()
    if path_has_link(output) or path_has_link(output.parent):
        raise RuntimeError("Путь резервной копии PostgreSQL содержит symlink или reparse point.")
    output = output.resolve(strict=False)
    repository_root = repository_root.resolve(strict=True)
    try:
        output.relative_to(repository_root)
    except ValueError:
        pass
    else:
        raise RuntimeError("Резервная копия PostgreSQL должна находиться вне репозитория.")
    output.parent.mkdir(parents=True, exist_ok=True)
    if is_unsafe_path(output) or not output.parent.is_dir():
        raise RuntimeError("Каталог резервной копии PostgreSQL небезопасен.")
    if os.path.lexists(str(output)):
        raise RuntimeError("Файл резервной копии уже существует.")
    return output


def _backup(
    settings: DatabaseSettings,
    output: Path,
    distro: str,
    repository_root: Path,
    *,
    transport: str = "docker",
) -> Path:
    output = _validate_external_output(output, repository_root)
    passfile = os.environ.get("AZURPILOT_POSTGRES_MIGRATOR_PGPASSFILE") or os.environ.get(
        "AZURPILOT_POSTGRES_PGPASSFILE"
    )
    maintenance = (
        _maintenance_settings(settings) if transport in {"native", "wsl"} else None
    )
    sslrootcert = (
        os.environ.get("PGSSLROOTCERT") if maintenance is not None else None
    )
    environment = _backup_process_environment(
        passfile=passfile,
        sslmode=maintenance.sslmode if maintenance is not None else None,
        sslrootcert=sslrootcert,
    )

    if transport == "docker":
        _require_docker_endpoint(settings, repository_root)
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
        assert maintenance is not None
        if transport == "native":
            native = shutil.which("pg_dump")
            if native is None:
                raise RuntimeError("Системный pg_dump недоступен для резервного копирования PostgreSQL.")
            arguments = [native, *_pg_dump_arguments(maintenance)]
            restore = shutil.which("pg_restore")
            if restore is None:
                raise RuntimeError("Системный pg_restore недоступен для резервного копирования PostgreSQL.")
            restore_arguments = [restore, "--list", "{temporary}"]
        else:
            wsl_environment = [
                "PGPASSFILE="
                + os.environ.get("AZURPILOT_WSL_PGPASSFILE", "/etc/azurpilot/pgpass"),
                f"PGSSLMODE={maintenance.sslmode}",
            ]
            if sslrootcert:
                wsl_environment.append(f"PGSSLROOTCERT={sslrootcert}")
            arguments = [
                "wsl.exe",
                "--distribution",
                distro,
                "--exec",
                "env",
                *wsl_environment,
                "pg_dump",
                *_pg_dump_arguments(maintenance),
            ]
            restore_arguments = [
                "wsl.exe",
                "--distribution",
                distro,
                "--exec",
                "pg_restore",
                "--list",
                "{temporary_wsl}",
            ]
    else:
        raise ValueError("Транспорт резервного копирования PostgreSQL не поддерживается.")

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
                _run_hidden(
                    restore_arguments,
                    stdin=stream,
                    environment=environment,
                )
        else:
            format_arguments = {"temporary": str(temporary)}
            if transport == "wsl":
                format_arguments["temporary_wsl"] = _wsl_path(temporary)
            restore_arguments = [
                argument.format(**format_arguments)
                for argument in restore_arguments
            ]
            _run_hidden(restore_arguments, environment=environment)
        os.link(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


def backup_for_repository(
    repository_root: str | Path,
    output: str | Path,
    *,
    transport: str = "docker",
    distro: str = "Archlinux",
) -> Path:
    """Создать и проверить logical backup для произвольного checkout.

    В отличие от CLI-обёртки функция не использует глобальный
    ``tools.paths.REPOSITORY_ROOT``: это важно для disposable update fixtures.
    Секреты берутся только canonical PostgreSQL seam и не попадают в argv.
    """

    raw_root = Path(repository_root).expanduser()
    if path_has_link(raw_root):
        raise RuntimeError("Корень репозитория для резервного копирования содержит symlink или reparse point.")
    root = raw_root.resolve(strict=True)
    marker = root / DEFAULT_BACKEND_MARKER_PATH
    if path_has_link(marker) or not marker.is_file():
        raise RuntimeError("Рабочий маркер backend отсутствует для резервного копирования.")
    settings = DatabaseSettings.from_backend_marker(marker)
    destination = Path(output).expanduser()
    return _backup(settings, destination, distro, root, transport=transport)


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
    """Разрешить только штатную роль migrator на конечной точке рабочего маркера."""

    if migrator_settings.user != "azurpilot_migrator":
        raise StorageConfigurationError(
            "Обновление рабочей схемы требует роль azurpilot_migrator."
        )
    if (
        marker_settings.host != migrator_settings.host
        or marker_settings.port != migrator_settings.port
        or marker_settings.database != migrator_settings.database
        or marker_settings.sslmode != migrator_settings.sslmode
        or marker_settings.runtime_timezone != migrator_settings.runtime_timezone
    ):
        raise StorageConfigurationError(
            "Конечная точка migrator не совпадает с рабочим маркером backend."
        )


def _require_upgrade_marker_revision(
    configuration: Config,
    marker_head: str,
) -> None:
    """Разрешить только известную ревизию-предка текущего Alembic head."""

    scripts = ScriptDirectory.from_config(configuration)
    if set(scripts.get_heads()) != {EXPECTED_ALEMBIC_HEAD}:
        raise StorageConfigurationError(
            "Граф Alembic не соответствует ожидаемому заголовку рабочей схемы."
        )
    allowed_revisions = {
        script.revision
        for script in scripts.iterate_revisions(EXPECTED_ALEMBIC_HEAD, "base")
        if script.revision is not None
    }
    allowed_revisions.add(EXPECTED_ALEMBIC_HEAD)
    if marker_head not in allowed_revisions:
        raise StorageConfigurationError(
            "Рабочий маркер backend содержит неизвестный или недопустимый заголовок схемы."
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
    """Выполнить обновление migrator в отдельном процессе, не меняя среду приложения."""

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
            "Штатное обновление схемы PostgreSQL превысило 180 секунд."
        ) from exc
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        if details.startswith("Ошибка рабочего PostgreSQL:"):
            details = details.removeprefix("Ошибка рабочего PostgreSQL:").strip()
        if not details:
            details = f"код завершения {result.returncode}"
        raise RuntimeError(f"Штатное обновление схемы PostgreSQL не выполнено: {details}")


def _prepare(
    marker: Path = _REPOSITORY_ROOT / DEFAULT_BACKEND_MARKER_PATH,
) -> None:
    """Подготовить рабочий PostgreSQL к запуску без смешивания ролей приложения и migrator."""

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
        description="Эксплуатационные команды рабочего PostgreSQL AzurPilot."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    health = subparsers.add_parser("health", help="Проверить маркер, доступ и заголовок схемы.")
    health.add_argument("--marker", default=str(DEFAULT_BACKEND_MARKER_PATH))

    prepare = subparsers.add_parser(
        "prepare",
        help="Подготовить схему и проверить доступ приложения перед запуском.",
    )
    prepare.add_argument("--marker", default=str(DEFAULT_BACKEND_MARKER_PATH))

    backup = subparsers.add_parser("backup", help="Создать проверяемый пользовательский дамп.")
    backup.add_argument("--marker", default=str(DEFAULT_BACKEND_MARKER_PATH))
    backup.add_argument("--output", required=True)
    backup.add_argument("--distro", default="Archlinux")
    backup.add_argument(
        "--transport",
        choices=("docker", "native", "wsl"),
        default="docker",
        help="Источник pg_dump: Docker Compose по умолчанию или транспорт отката.",
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
            "Ошибка рабочего PostgreSQL: операция с базой данных завершилась ошибкой.",
            file=sys.stderr,
        )
        return 1
    except (OSError, RuntimeError, StorageError, ValueError) as exc:
        print(f"Ошибка рабочего PostgreSQL: {exc}", file=sys.stderr)
        return 1
    print("Операция рабочего PostgreSQL завершена успешно.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
