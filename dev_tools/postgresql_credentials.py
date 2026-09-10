"""Guarded-ротация локальных production credentials PostgreSQL."""

from __future__ import annotations

import argparse
import os
import re
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

from module.persistence.local_environment_schema import (
    INFRASTRUCTURE_ENVIRONMENT_KEYS,
    LOCAL_ENVIRONMENT_KEYS,
    POSTGRES_ENVIRONMENT_KEYS,
    WSL_ENVIRONMENT_KEYS,
)

CONFIRMATION = "ROTATE-AZURPILOT-POSTGRESQL-CREDENTIALS"
_SAFE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_SAFE_WSL_PASSFILE = re.compile(r"^/etc/azurpilot/[A-Za-z0-9._-]+$")
_ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_OWNED_ENV_KEYS = POSTGRES_ENVIRONMENT_KEYS | WSL_ENVIRONMENT_KEYS
_PRESERVED_ENV_KEYS = INFRASTRUCTURE_ENVIRONMENT_KEYS
_ROLE_CONTRACT = {
    "azurpilot_app": (True, False, False, False),
    "azurpilot_migrator": (True, False, False, False),
    "azurpilot_owner": (False, False, False, False),
}


def _run(
    arguments: list[str],
    *,
    input_bytes: bytes | None = None,
    capture: bool = False,
    capture_stderr: bool = False,
    expected: frozenset[int] = frozenset({0}),
) -> subprocess.CompletedProcess[bytes]:
    options: dict[str, object] = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    completed = subprocess.run(
        arguments,
        input=input_bytes,
        stdin=None if input_bytes is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.PIPE if capture_stderr else subprocess.DEVNULL,
        check=False,
        timeout=180,
        **options,
    )
    if completed.returncode not in expected:
        raise RuntimeError("Операция credential lifecycle завершилась ошибкой.")
    return completed


def _wsl(distro: str, *arguments: str) -> list[str]:
    return ["wsl.exe", "--distribution", distro, "--exec", *arguments]


def _read_wsl_file(distro: str, path: str) -> bytes:
    return _run(_wsl(distro, "sudo", "cat", path), capture=True).stdout


def _require_wsl_passfile(distro: str, path: str, owner: str) -> None:
    if not _SAFE_WSL_PASSFILE.fullmatch(path):
        raise RuntimeError("WSL passfile использует небезопасный путь.")
    metadata = _run(
        _wsl(distro, "sudo", "stat", "--format=%f|%a|%U", "--", path),
        capture=True,
    ).stdout.decode().strip()
    try:
        raw_mode, permissions, observed_owner = metadata.split("|", 2)
        regular_file = int(raw_mode, 16) & 0o170000 == 0o100000
    except ValueError as exc:
        raise RuntimeError("WSL passfile отсутствует или небезопасен.") from exc
    if not regular_file or permissions != "600" or observed_owner != owner:
        raise RuntimeError("WSL passfile отсутствует или небезопасен.")


def _write_wsl_file(distro: str, path: str, owner: str, content: bytes) -> None:
    temporary = f"{path}.{os.getpid()}.tmp"
    try:
        _run(
            _wsl(
                distro,
                "sudo",
                "install",
                "-m",
                "600",
                "-o",
                owner,
                "-g",
                owner,
                "/dev/null",
                temporary,
            )
        )
        _run(_wsl(distro, "sudo", "tee", temporary), input_bytes=content)
        _run(_wsl(distro, "sudo", "sync", "-f", temporary))
        _run(_wsl(distro, "sudo", "mv", "-T", temporary, path))
    finally:
        _run(
            _wsl(distro, "sudo", "rm", "-f", temporary),
            expected=frozenset({0, 1}),
        )


def _restrict_windows_file(path: Path) -> None:
    identity = _run(["whoami.exe"], capture=True).stdout.decode().strip()
    _run(
        [
            "icacls.exe",
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"{identity}:(F)",
            "/grant:r",
            "SYSTEM:(F)",
        ]
    )


def _write_windows_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.close(descriptor)
        _restrict_windows_file(temporary)
        with temporary.open("wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _restrict_windows_file(path)
    finally:
        temporary.unlink(missing_ok=True)


def _create_wsl_private_tempdir(distro: str, owner: str) -> str:
    path = (
        _run(
            _wsl(distro, "mktemp", "--directory", "/tmp/azurpilot-credentials.XXXXXXXXXX"),
            capture=True,
        )
        .stdout.decode()
        .strip()
    )
    if not re.fullmatch(r"/tmp/azurpilot-credentials\.[A-Za-z0-9]{10}", path):
        raise RuntimeError("Временный каталог credentials имеет небезопасный путь.")
    metadata = _run(
        _wsl(distro, "stat", "--format=%a:%U", path), capture=True
    ).stdout.decode().strip()
    if metadata != f"700:{owner}":
        _run(
            _wsl(distro, "rm", "-rf", "--", path),
            expected=frozenset({0, 1}),
        )
        raise RuntimeError("Временный каталог credentials имеет небезопасные права.")
    return path


def _parse_pgpass_line(raw_line: str) -> list[str] | None:
    fields: list[str] = []
    current: list[str] = []
    escaped = False
    for character in raw_line:
        if escaped:
            if character not in {":", "\\"}:
                current.append("\\")
            current.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == ":" and len(fields) < 4:
            fields.append("".join(current))
            current = []
        else:
            current.append(character)
    if escaped or len(fields) != 4:
        return None
    fields.append("".join(current))
    return fields


def _escape_pgpass_field(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:")


def _password_for(content: bytes, database: str, role: str) -> str:
    for raw_line in content.decode("utf-8").splitlines():
        fields = _parse_pgpass_line(raw_line)
        if fields is None:
            continue
        if fields[2] in {database, "*"} and fields[3] == role and fields[4]:
            return fields[4]
    raise RuntimeError("Существующий passfile не содержит production role.")


def _sql_secret(value: str) -> str:
    if not value or "\x00" in value or "\r" in value or "\n" in value:
        raise RuntimeError("Формат PostgreSQL secret некорректен.")
    return value.replace("'", "''")


def _alter_roles(distro: str, app_secret: str, migrator_secret: str) -> None:
    app_sql = _sql_secret(app_secret)
    migrator_sql = _sql_secret(migrator_secret)
    sql = (
        "BEGIN;\n"
        "SET password_encryption = 'scram-sha-256';\n"
        f"ALTER ROLE azurpilot_app PASSWORD '{app_sql}';\n"
        f"ALTER ROLE azurpilot_migrator PASSWORD '{migrator_sql}';\n"
        "COMMIT;\n"
    ).encode()
    _run(
        _wsl(
            distro,
            "sudo",
            "-u",
            "postgres",
            "psql",
            "-X",
            "--no-psqlrc",
            "--set",
            "ON_ERROR_STOP=1",
            "--dbname",
            "postgres",
        ),
        input_bytes=sql,
    )


def _require_role_contract(distro: str) -> None:
    sql = (
        "select rolname,rolcanlogin,rolsuper,rolcreatedb,rolcreaterole "
        "from pg_roles where rolname in "
        "('azurpilot_app','azurpilot_migrator','azurpilot_owner') order by rolname;"
    )
    output = _run(
        _wsl(
            distro,
            "sudo",
            "-u",
            "postgres",
            "psql",
            "-X",
            "-A",
            "-t",
            "-F",
            "|",
            "--dbname",
            "postgres",
            "--command",
            sql,
        ),
        capture=True,
    ).stdout.decode()
    observed: dict[str, tuple[bool, bool, bool, bool]] = {}
    for line in output.splitlines():
        fields = line.split("|")
        if len(fields) == 5:
            observed[fields[0]] = tuple(value == "t" for value in fields[1:])  # type: ignore[assignment]
    if observed != _ROLE_CONTRACT:
        raise RuntimeError("Production roles не соответствуют least-privilege contract.")


def _verify_backup(distro: str, backup: Path) -> None:
    resolved = backup.resolve(strict=True)
    if not resolved.is_file() or resolved.stat().st_size < 1024:
        raise RuntimeError("Проверенная резервная копия PostgreSQL отсутствует.")
    drive = resolved.drive.rstrip(":").lower()
    if len(drive) != 1:
        raise RuntimeError(
            "Резервная копия PostgreSQL должна использовать локальный путь с буквой диска."
        )
    suffix = resolved.as_posix().split(":", 1)[1]
    _run(_wsl(distro, "pg_restore", "--list", f"/mnt/{drive}{suffix}"))


def _passfile(database: str, app_secret: str, migrator_secret: str) -> bytes:
    database_field = _escape_pgpass_field(database)
    app_field = _escape_pgpass_field(app_secret)
    migrator_field = _escape_pgpass_field(migrator_secret)
    return (
        f"127.0.0.1:5432:{database_field}:azurpilot_app:{app_field}\n"
        f"localhost:5432:{database_field}:azurpilot_app:{app_field}\n"
        f"127.0.0.1:5432:*:azurpilot_migrator:{migrator_field}\n"
    ).encode()


def _merge_windows_passfile(
    previous: bytes,
    database: str,
    app_secret: str,
    migrator_secret: str,
) -> bytes:
    preserved: list[str] = []
    for line in previous.decode("utf-8").splitlines():
        fields = _parse_pgpass_line(line)
        if (
            fields is not None
            and fields[0] in {"127.0.0.1", "localhost"}
            and fields[1] == "5432"
            and fields[2] in {database, "*"}
            and fields[3] in {"azurpilot_app", "azurpilot_migrator"}
        ):
            continue
        preserved.append(line)
    additions = _passfile(database, app_secret, migrator_secret).decode().splitlines()
    return ("\n".join([*preserved, *additions]) + "\n").encode()


def _env_document(
    repository: Path,
    windows_passfile: Path,
    wsl_passfile: str,
    distro: str,
    database: str,
    app_secret: str,
    migrator_secret: str,
) -> bytes:
    if repository.is_symlink() or not repository.is_dir():
        raise RuntimeError("Корень репозитория отсутствует или небезопасен.")
    values = {
        "AZURPILOT_POSTGRES_HOST": "127.0.0.1",
        "AZURPILOT_POSTGRES_PORT": "5432",
        "AZURPILOT_POSTGRES_DATABASE": database,
        "AZURPILOT_POSTGRES_USER": "azurpilot_app",
        "AZURPILOT_POSTGRES_PASSWORD": app_secret,
        "AZURPILOT_POSTGRES_SSLMODE": "disable",
        "AZURPILOT_POSTGRES_RUNTIME_TIMEZONE": "Asia/Novosibirsk",
        "AZURPILOT_POSTGRES_PGPASSFILE": str(windows_passfile),
        "AZURPILOT_POSTGRES_MIGRATOR_HOST": "127.0.0.1",
        "AZURPILOT_POSTGRES_MIGRATOR_PORT": "5432",
        "AZURPILOT_POSTGRES_MIGRATOR_DATABASE": database,
        "AZURPILOT_POSTGRES_MIGRATOR_USER": "azurpilot_migrator",
        "AZURPILOT_POSTGRES_MIGRATOR_PASSWORD": migrator_secret,
        "AZURPILOT_POSTGRES_MIGRATOR_SSLMODE": "disable",
        "AZURPILOT_POSTGRES_MIGRATOR_RUNTIME_TIMEZONE": "Asia/Novosibirsk",
        "AZURPILOT_POSTGRES_MIGRATOR_PGPASSFILE": str(windows_passfile),
        "AZURPILOT_WSL_DISTRO": distro,
        "AZURPILOT_WSL_PGPASSFILE": wsl_passfile,
    }
    if any(
        not value
        or "\x00" in value
        or "\r" in value
        or "\n" in value
        or " #" in value
        for value in values.values()
    ):
        raise RuntimeError("Значение локального PostgreSQL env некорректно.")
    return ("".join(f"{key}={value}\n" for key, value in values.items())).encode()


def _env_key(raw_line: str) -> str | None:
    line = raw_line.strip()
    if not line or line.startswith("#"):
        return None
    if "=" not in line:
        raise RuntimeError("Локальный env содержит некорректную строку.")
    key, _raw_value = line.split("=", 1)
    key = key.strip()
    if not _ENV_KEY_RE.fullmatch(key):
        raise RuntimeError("Локальный env содержит некорректный ключ.")
    return key


def _read_env_document(path: Path) -> bytes | None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeError("Локальный env отсутствует или небезопасен.")
    return path.read_bytes() if path.is_file() else None


def _merge_env_document(previous: bytes | None, generated: bytes) -> bytes:
    if previous is None:
        return generated
    try:
        previous_text = previous.decode("utf-8")
        generated_text = generated.decode("utf-8")
    except UnicodeError as exc:
        raise RuntimeError("Локальный env невозможно прочитать как UTF-8.") from exc

    generated_keys: set[str] = set()
    for raw_line in generated_text.splitlines():
        key = _env_key(raw_line)
        if key is None or key in generated_keys:
            raise RuntimeError("Сгенерированный PostgreSQL env некорректен.")
        generated_keys.add(key)

    preserved: list[str] = []
    seen_keys: set[str] = set()
    for raw_line in previous_text.splitlines():
        key = _env_key(raw_line)
        if key is None:
            preserved.append(raw_line)
            continue
        if key in seen_keys:
            raise RuntimeError("Локальный env содержит дублирующийся ключ.")
        seen_keys.add(key)
        if key in generated_keys:
            continue
        if key in _PRESERVED_ENV_KEYS:
            preserved.append(raw_line)
            continue
        if key in _OWNED_ENV_KEYS:
            raise RuntimeError(
                "Локальный env содержит неподдерживаемый ключ PostgreSQL/WSL."
            )
        if key.startswith("AZURPILOT_") and key not in LOCAL_ENVIRONMENT_KEYS:
            raise RuntimeError(
                "Локальный env содержит неизвестный ключ AzurPilot environment."
            )
        preserved.append(raw_line)

    while preserved and not preserved[-1].strip():
        preserved.pop()
    prefix = "\n".join(preserved)
    if prefix:
        prefix += "\n"
    if not generated_text.endswith("\n"):
        generated_text += "\n"
    return (prefix + generated_text).encode("utf-8")


def _auth(
    distro: str,
    passfile: str,
    database: str,
    role: str,
    *,
    should_succeed: bool,
) -> None:
    expected = frozenset({0}) if should_succeed else frozenset({2})
    completed = _run(
        _wsl(
            distro,
            "env",
            "LC_ALL=C",
            f"PGPASSFILE={passfile}",
            "psql",
            "-X",
            "--no-psqlrc",
            "--host",
            "127.0.0.1",
            "--port",
            "5432",
            "--username",
            role,
            "--dbname",
            database,
            "--command",
            "select 1",
        ),
        capture_stderr=not should_succeed,
        expected=expected,
    )
    if not should_succeed and b"password authentication failed for user" not in (
        completed.stderr or b""
    ):
        raise RuntimeError("Результат PostgreSQL auth test не соответствует ожиданию.")


def rotate(arguments: argparse.Namespace) -> None:
    if arguments.confirm != CONFIRMATION:
        raise RuntimeError("Точное подтверждение ротации credentials отсутствует.")
    if not _SAFE_NAME.fullmatch(arguments.database):
        raise RuntimeError("Имя production database некорректно.")
    if not _SAFE_WSL_PASSFILE.fullmatch(arguments.wsl_passfile):
        raise RuntimeError("WSL passfile использует небезопасный путь.")
    repository_argument = Path(arguments.repository_root)
    if repository_argument.is_symlink() or not repository_argument.is_dir():
        raise RuntimeError("Корень репозитория отсутствует или небезопасен.")
    repository = repository_argument.resolve(strict=True)
    env_path = repository / ".env"
    windows_passfile_argument = arguments.windows_passfile
    if windows_passfile_argument is None:
        appdata = os.environ.get("APPDATA", "").strip()
        if not appdata:
            raise RuntimeError("APPDATA для Windows passfile не задан.")
        windows_passfile_argument = str(Path(appdata) / "postgresql/pgpass.conf")
    candidate_passfile = Path(windows_passfile_argument)
    if not candidate_passfile.is_absolute():
        raise RuntimeError("Windows passfile должен использовать абсолютный путь.")
    windows_passfile = candidate_passfile.resolve()
    try:
        windows_passfile.relative_to(repository)
    except ValueError:
        pass
    else:
        raise RuntimeError("Windows passfile должен находиться вне репозитория.")
    _verify_backup(arguments.distro, Path(arguments.verified_backup))
    _require_role_contract(arguments.distro)

    wsl_user = _run(
        _wsl(arguments.distro, "id", "-un"), capture=True
    ).stdout.decode().strip()
    _require_wsl_passfile(arguments.distro, arguments.wsl_passfile, wsl_user)
    old_wsl = _read_wsl_file(arguments.distro, arguments.wsl_passfile)
    old_app = _password_for(old_wsl, arguments.database, "azurpilot_app")
    old_migrator = _password_for(old_wsl, arguments.database, "azurpilot_migrator")
    if windows_passfile.is_symlink() or (
        windows_passfile.exists() and not windows_passfile.is_file()
    ):
        raise RuntimeError("Windows passfile отсутствует или небезопасен.")
    windows_existed = windows_passfile.is_file()
    old_windows = windows_passfile.read_bytes() if windows_existed else b""
    old_env = _read_env_document(env_path)
    app_secret = secrets.token_urlsafe(48)
    migrator_secret = secrets.token_urlsafe(48)
    if (
        app_secret == migrator_secret
        or not re.fullmatch(r"[A-Za-z0-9_-]{48,}", app_secret)
        or not re.fullmatch(r"[A-Za-z0-9_-]{48,}", migrator_secret)
    ):
        raise RuntimeError("Генератор PostgreSQL secrets вернул совпадение.")
    new_passfile = _passfile(arguments.database, app_secret, migrator_secret)
    new_env = _merge_env_document(
        old_env,
        _env_document(
            repository,
            windows_passfile,
            arguments.wsl_passfile,
            arguments.distro,
            arguments.database,
            app_secret,
            migrator_secret,
        ),
    )
    try:
        _alter_roles(arguments.distro, app_secret, migrator_secret)
        _write_wsl_file(
            arguments.distro, arguments.wsl_passfile, wsl_user, new_passfile
        )
        _write_windows_file(
            windows_passfile,
            _merge_windows_passfile(
                old_windows, arguments.database, app_secret, migrator_secret
            ),
        )
        _write_windows_file(env_path, new_env)
        _auth(
            arguments.distro,
            arguments.wsl_passfile,
            arguments.database,
            "azurpilot_app",
            should_succeed=True,
        )
        _auth(
            arguments.distro,
            arguments.wsl_passfile,
            arguments.database,
            "azurpilot_migrator",
            should_succeed=True,
        )
        old_test_directory = _create_wsl_private_tempdir(arguments.distro, wsl_user)
        old_test_path = f"{old_test_directory}/pgpass"
        try:
            _write_wsl_file(
                arguments.distro,
                old_test_path,
                wsl_user,
                _passfile(arguments.database, old_app, old_migrator),
            )
            _auth(
                arguments.distro,
                old_test_path,
                arguments.database,
                "azurpilot_app",
                should_succeed=False,
            )
            _auth(
                arguments.distro,
                old_test_path,
                arguments.database,
                "azurpilot_migrator",
                should_succeed=False,
            )
        finally:
            _run(
                _wsl(
                    arguments.distro,
                    "sudo",
                    "rm",
                    "-rf",
                    "--",
                    old_test_directory,
                )
            )
    except Exception:
        try:
            _alter_roles(arguments.distro, old_app, old_migrator)
            _write_wsl_file(
                arguments.distro, arguments.wsl_passfile, wsl_user, old_wsl
            )
            if windows_existed:
                _write_windows_file(windows_passfile, old_windows)
            else:
                windows_passfile.unlink(missing_ok=True)
            if old_env is None:
                env_path.unlink(missing_ok=True)
            else:
                _write_windows_file(env_path, old_env)
        except Exception as rollback_exc:
            raise RuntimeError(
                "Ротация credentials и автоматический rollback завершились ошибкой."
            ) from rollback_exc
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ротация app/migrator credentials без password в argv."
    )
    parser.add_argument("--confirm", required=True)
    parser.add_argument("--verified-backup", required=True)
    parser.add_argument("--repository-root", default=".")
    parser.add_argument("--distro", default="archlinux")
    parser.add_argument("--database", default="azurpilot")
    parser.add_argument("--wsl-passfile", default="/etc/azurpilot/pgpass")
    parser.add_argument(
        "--windows-passfile",
        default=None,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        rotate(_parser().parse_args(argv))
    except (
        OSError,
        RuntimeError,
        UnicodeError,
        ValueError,
        subprocess.SubprocessError,
    ):
        print("Ротация PostgreSQL credentials завершилась ошибкой.", file=sys.stderr)
        return 1
    print("Ротация PostgreSQL credentials и auth tests завершены успешно.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
