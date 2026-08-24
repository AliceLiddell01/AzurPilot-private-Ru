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

CONFIRMATION = "ROTATE-AZURPILOT-POSTGRESQL-CREDENTIALS"
_SAFE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
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
        stderr=subprocess.DEVNULL,
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


def _write_wsl_file(distro: str, path: str, owner: str, content: bytes) -> None:
    temporary = f"{path}.{os.getpid()}.tmp"
    try:
        _run(_wsl(distro, "sudo", "tee", temporary), input_bytes=content)
        _run(_wsl(distro, "sudo", "chown", f"{owner}:{owner}", temporary))
        _run(_wsl(distro, "sudo", "chmod", "600", temporary))
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


def _password_for(content: bytes, database: str, role: str) -> str:
    for raw_line in content.decode("utf-8").splitlines():
        fields = raw_line.split(":", 4)
        if len(fields) != 5:
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
    suffix = resolved.as_posix().split(":", 1)[1]
    _run(_wsl(distro, "pg_restore", "--list", f"/mnt/{drive}{suffix}"))


def _passfile(database: str, app_secret: str, migrator_secret: str) -> bytes:
    return (
        f"127.0.0.1:5432:{database}:azurpilot_app:{app_secret}\n"
        f"localhost:5432:{database}:azurpilot_app:{app_secret}\n"
        f"127.0.0.1:5432:*:azurpilot_migrator:{migrator_secret}\n"
    ).encode()


def _merge_windows_passfile(
    previous: bytes,
    database: str,
    app_secret: str,
    migrator_secret: str,
) -> bytes:
    preserved: list[str] = []
    for line in previous.decode("utf-8").splitlines():
        fields = line.split(":", 4)
        if len(fields) == 5 and fields[3] in {"azurpilot_app", "azurpilot_migrator"}:
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
    if repository.is_symlink() or not repository.is_dir():
        raise RuntimeError("Корень репозитория отсутствует или небезопасен.")
    return ("".join(f"{key}={value}\n" for key, value in values.items())).encode()


def _auth(
    distro: str,
    passfile: str,
    database: str,
    role: str,
    *,
    should_succeed: bool,
) -> None:
    completed = _run(
        _wsl(
            distro,
            "env",
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
        expected=frozenset({0, 1, 2}),
    )
    if (completed.returncode == 0) is not should_succeed:
        raise RuntimeError("Результат PostgreSQL auth test не соответствует ожиданию.")


def rotate(arguments: argparse.Namespace) -> None:
    if arguments.confirm != CONFIRMATION:
        raise RuntimeError("Точное подтверждение ротации credentials отсутствует.")
    if not _SAFE_NAME.fullmatch(arguments.database):
        raise RuntimeError("Имя production database некорректно.")
    repository = Path(arguments.repository_root).resolve(strict=True)
    env_path = repository / ".env"
    windows_passfile = Path(arguments.windows_passfile).resolve()
    _verify_backup(arguments.distro, Path(arguments.verified_backup))
    _require_role_contract(arguments.distro)

    wsl_user = _run(
        _wsl(arguments.distro, "id", "-un"), capture=True
    ).stdout.decode().strip()
    old_wsl = _read_wsl_file(arguments.distro, arguments.wsl_passfile)
    old_app = _password_for(old_wsl, arguments.database, "azurpilot_app")
    old_migrator = _password_for(old_wsl, arguments.database, "azurpilot_migrator")
    old_windows = windows_passfile.read_bytes() if windows_passfile.is_file() else b""
    old_env = env_path.read_bytes() if env_path.is_file() else None
    app_secret = secrets.token_urlsafe(48)
    migrator_secret = secrets.token_urlsafe(48)
    if (
        app_secret == migrator_secret
        or not re.fullmatch(r"[A-Za-z0-9_-]{48,}", app_secret)
        or not re.fullmatch(r"[A-Za-z0-9_-]{48,}", migrator_secret)
    ):
        raise RuntimeError("Генератор PostgreSQL secrets вернул совпадение.")
    new_passfile = _passfile(arguments.database, app_secret, migrator_secret)
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
        _write_windows_file(
            env_path,
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
        old_test_path = f"/tmp/azurpilot-old-pgpass-{os.getpid()}"
        _write_wsl_file(
            arguments.distro,
            old_test_path,
            wsl_user,
            _passfile(arguments.database, old_app, old_migrator),
        )
        try:
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
            _run(_wsl(arguments.distro, "sudo", "rm", "-f", old_test_path))
    except Exception:
        try:
            _alter_roles(arguments.distro, old_app, old_migrator)
            _write_wsl_file(
                arguments.distro, arguments.wsl_passfile, wsl_user, old_wsl
            )
            _write_windows_file(windows_passfile, old_windows)
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
        default=str(Path(os.environ.get("APPDATA", "")) / "postgresql/pgpass.conf"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        rotate(_parser().parse_args(argv))
    except (OSError, RuntimeError, UnicodeError, ValueError):
        print("Ротация PostgreSQL credentials завершилась ошибкой.", file=sys.stderr)
        return 1
    print("Ротация PostgreSQL credentials и auth tests завершены успешно.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
