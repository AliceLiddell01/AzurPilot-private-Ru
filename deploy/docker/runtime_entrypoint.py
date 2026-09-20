"""Безопасно подготовить файлы только для запуска WebUI."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_MAX_RUNTIME_FILE_SIZE = 65_536
_ENV_SOURCE = Path("/run/secrets/azurpilot.env")
_PASSFILE_SOURCE = Path("/run/secrets/azurpilot.pgpass")
_MARKER_SOURCE = Path("/run/secrets/storage_backend.json")
_RUNTIME_DIRECTORY = Path("/run/azurpilot")
_ENV_TARGET = _RUNTIME_DIRECTORY / ".env"
_PASSFILE_TARGET = _RUNTIME_DIRECTORY / "pgpass.conf"
_MARKER_TARGET = _RUNTIME_DIRECTORY / "storage_backend.json"
_ENV_PATH_VARIABLE = "AZURPILOT_LOCAL_ENV_PATH"
_MARKER_PATH_VARIABLE = "AZURPILOT_BACKEND_MARKER_PATH"
_DOCKER_POSTGRES_HOST_VARIABLE = "AZURPILOT_DOCKER_POSTGRES_HOST"
_DOCKER_POSTGRES_PORT_VARIABLE = "AZURPILOT_DOCKER_POSTGRES_PORT"
_DOCKER_REDIS_HOST_VARIABLE = "AZURPILOT_DOCKER_REDIS_HOST"
_DOCKER_REDIS_PORT_VARIABLE = "AZURPILOT_DOCKER_REDIS_PORT"
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_APP_ROLE = "azurpilot_app"
_MIGRATOR_ROLE = "azurpilot_migrator"
_PASSFILE_IDENTITIES = {
    "AZURPILOT_POSTGRES_HOST",
    "AZURPILOT_POSTGRES_PORT",
    "AZURPILOT_POSTGRES_DATABASE",
    "AZURPILOT_POSTGRES_USER",
    "AZURPILOT_POSTGRES_MIGRATOR_HOST",
    "AZURPILOT_POSTGRES_MIGRATOR_PORT",
    "AZURPILOT_POSTGRES_MIGRATOR_DATABASE",
    "AZURPILOT_POSTGRES_MIGRATOR_USER",
}
_REDIS_TRANSPORT_IDENTITIES = {
    "AZURPILOT_REDIS_HOST",
    "AZURPILOT_REDIS_PORT",
}


def _read_source(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("Исходный файл среды выполнения Docker отсутствует или небезопасен.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        with os.fdopen(descriptor, "rb") as source:
            payload = source.read(_MAX_RUNTIME_FILE_SIZE + 1)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    if len(payload) > _MAX_RUNTIME_FILE_SIZE:
        raise RuntimeError("Исходный файл среды выполнения Docker превышает допустимый размер.")
    return payload


def _replace_passfile_paths(payload: bytes) -> bytes:
    text = payload.decode("utf-8")
    lines: list[str] = []
    for raw_line in text.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        ending = raw_line[len(line) :]
        if line.lstrip().startswith("#") or "=" not in line:
            lines.append(raw_line)
            continue
        key, _value = line.split("=", 1)
        if key.strip().endswith("_PGPASSFILE"):
            line = f"{key}=/run/secrets/azurpilot.pgpass"
        lines.append(line + ending)
    return "".join(lines).encode("utf-8")


def _replace_redis_transport(payload: bytes) -> bytes:
    """Перевести staged Redis endpoint на service DNS без изменения credentials."""

    transport = _docker_redis_transport()
    if transport is None:
        return payload
    try:
        text = payload.decode("utf-8")
    except UnicodeError as exc:
        raise RuntimeError("Локальный Docker env невозможно безопасно прочитать.") from exc
    values: dict[str, str] = {}
    lines: list[str] = []
    for raw_line in text.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        ending = raw_line[len(line) :]
        if line.lstrip().startswith("#") or "=" not in line:
            lines.append(raw_line)
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if key not in _REDIS_TRANSPORT_IDENTITIES:
            lines.append(raw_line)
            continue
        if key in values:
            raise RuntimeError("Локальный Docker env содержит дублирующийся Redis endpoint.")
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if not value or any(character in value for character in "\x00\r\n"):
            raise RuntimeError("Локальный Docker env содержит некорректный Redis endpoint.")
        values[key] = value
        if key == "AZURPILOT_REDIS_HOST":
            if value not in _LOOPBACK_HOSTS:
                raise RuntimeError("Локальный Docker env использует недопустимый Redis host.")
            replacement = transport[0]
        else:
            try:
                port = int(value)
            except ValueError as exc:
                raise RuntimeError("Локальный Docker env содержит некорректный Redis port.") from exc
            if not 1 <= port <= 65_535:
                raise RuntimeError("Локальный Docker env содержит некорректный Redis port.")
            replacement = str(transport[1])
        lines.append(f"{key}={replacement}{ending}")
    if _REDIS_TRANSPORT_IDENTITIES.difference(values):
        raise RuntimeError("Локальный Docker env не содержит полный Redis endpoint contract.")
    return "".join(lines).encode("utf-8")


def _docker_postgres_transport() -> tuple[str, int] | None:
    host = os.environ.get(_DOCKER_POSTGRES_HOST_VARIABLE)
    port = os.environ.get(_DOCKER_POSTGRES_PORT_VARIABLE)
    if host is None and port is None:
        return None
    if host != "postgres" or port != "5432":
        raise RuntimeError(
            "Docker PostgreSQL transport не соответствует каноническому Compose service."
        )
    return host, 5432


def _docker_redis_transport() -> tuple[str, int] | None:
    host = os.environ.get(_DOCKER_REDIS_HOST_VARIABLE)
    port = os.environ.get(_DOCKER_REDIS_PORT_VARIABLE)
    if host is None and port is None:
        return None
    if host != "redis" or port != "6379":
        raise RuntimeError(
            "Docker Redis transport не соответствует каноническому Compose service."
        )
    return host, 6379


def _env_identity(payload: bytes) -> dict[str, str]:
    try:
        text = payload.decode("utf-8")
    except UnicodeError as exc:
        raise RuntimeError("Локальный Docker env невозможно безопасно прочитать.") from exc
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if key not in _PASSFILE_IDENTITIES:
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if (
            not value
            or "\x00" in value
            or "\r" in value
            or "\n" in value
            or key in values
        ):
            raise RuntimeError("Локальный Docker env не содержит однозначный PostgreSQL contract.")
        values[key] = value
    if _PASSFILE_IDENTITIES.difference(values):
        raise RuntimeError("Локальный Docker env не содержит полный PostgreSQL contract.")
    for prefix, role in (
        ("AZURPILOT_POSTGRES_", _APP_ROLE),
        ("AZURPILOT_POSTGRES_MIGRATOR_", _MIGRATOR_ROLE),
    ):
        if values[prefix + "HOST"] not in _LOOPBACK_HOSTS:
            raise RuntimeError("Локальный Docker env использует недопустимый PostgreSQL host.")
        try:
            port = int(values[prefix + "PORT"])
        except ValueError as exc:
            raise RuntimeError("Локальный Docker env содержит некорректный PostgreSQL port.") from exc
        if not 1 <= port <= 65535 or values[prefix + "USER"] != role:
            raise RuntimeError("Локальный Docker env не соответствует PostgreSQL role contract.")
    if (
        values["AZURPILOT_POSTGRES_DATABASE"]
        != values["AZURPILOT_POSTGRES_MIGRATOR_DATABASE"]
    ):
        raise RuntimeError("Локальный Docker env содержит разные PostgreSQL databases.")
    return values


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


def _stage_docker_pgpass(payload: bytes, env_payload: bytes) -> bytes:
    """Оставить только app/migrator records и перевести их на service DNS."""

    transport = _docker_postgres_transport()
    if transport is None:
        return payload
    expected = _env_identity(env_payload)
    try:
        text = payload.decode("utf-8")
    except UnicodeError as exc:
        raise RuntimeError("PGPASSFILE среды выполнения Docker невозможно прочитать.") from exc
    candidates: dict[str, list[list[str]]] = {
        _APP_ROLE: [],
        _MIGRATOR_ROLE: [],
    }
    expected_by_role = {
        _APP_ROLE: (
            expected["AZURPILOT_POSTGRES_HOST"],
            expected["AZURPILOT_POSTGRES_PORT"],
            expected["AZURPILOT_POSTGRES_DATABASE"],
        ),
        _MIGRATOR_ROLE: (
            expected["AZURPILOT_POSTGRES_MIGRATOR_HOST"],
            expected["AZURPILOT_POSTGRES_MIGRATOR_PORT"],
            expected["AZURPILOT_POSTGRES_MIGRATOR_DATABASE"],
        ),
    }
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        fields = _parse_pgpass_line(raw_line)
        if fields is None or not fields[4]:
            raise RuntimeError("PGPASSFILE содержит некорректную запись PostgreSQL.")
        role = fields[3]
        if role not in candidates:
            continue
        host, port, database = expected_by_role[role]
        if (
            fields[0] != host
            or fields[1] != port
            or fields[2] not in ({database} if role == _APP_ROLE else {database, "*"})
        ):
            continue
        candidates[role].append(fields)

    selected: list[list[str]] = []
    for role in (_APP_ROLE, _MIGRATOR_ROLE):
        if not candidates[role]:
            raise RuntimeError("PGPASSFILE не содержит однозначные production role records.")
        passwords = {fields[4] for fields in candidates[role]}
        if len(passwords) != 1:
            raise RuntimeError("PGPASSFILE содержит неоднозначные production role records.")
        expected_database = expected_by_role[role][2]
        exact = [fields for fields in candidates[role] if fields[2] == expected_database]
        chosen = exact[0] if exact else candidates[role][0]
        selected.append(
            [
                transport[0],
                str(transport[1]),
                chosen[2],
                role,
                chosen[4],
            ]
        )
    return (
        "\n".join(
            ":".join(_escape_pgpass_field(field) for field in fields)
            for fields in selected
        )
        + "\n"
    ).encode("utf-8")


def _write_runtime_file(path: Path, payload: bytes) -> None:
    if not _RUNTIME_DIRECTORY.is_dir() or _RUNTIME_DIRECTORY.is_symlink():
        raise RuntimeError("Временная файловая система среды выполнения Docker отсутствует или небезопасна.")
    if path.exists() or path.is_symlink():
        metadata = path.stat()
        current_uid = getattr(os, "getuid", lambda: metadata.st_uid)()
        if (
            path.is_symlink()
            or not path.is_file()
            or metadata.st_uid != current_uid
            or metadata.st_mode & 0o077
        ):
            raise RuntimeError("Файл среды выполнения Docker уже существует с небезопасными правами.")
        flags = os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    else:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _prepare_runtime_files() -> None:
    transport = _docker_postgres_transport()
    redis_transport = _docker_redis_transport()
    if _ENV_SOURCE.exists() or _ENV_SOURCE.is_symlink():
        env_payload = _read_source(_ENV_SOURCE)
        if not (_PASSFILE_SOURCE.exists() and not _PASSFILE_SOURCE.is_symlink()):
            raise RuntimeError("PGPASSFILE среды выполнения Docker отсутствует или небезопасен.")
        passfile_payload = _read_source(_PASSFILE_SOURCE)
        if transport is not None:
            passfile_payload = _stage_docker_pgpass(passfile_payload, env_payload)
        _write_runtime_file(_PASSFILE_TARGET, passfile_payload)
        env_payload = _replace_passfile_paths(env_payload).replace(
            b"/run/secrets/azurpilot.pgpass",
            str(_PASSFILE_TARGET).encode("ascii"),
        )
        if redis_transport is not None:
            env_payload = _replace_redis_transport(env_payload)
        _write_runtime_file(_ENV_TARGET, env_payload)
        os.environ[_ENV_PATH_VARIABLE] = str(_ENV_TARGET)

    if _MARKER_SOURCE.exists() or _MARKER_SOURCE.is_symlink():
        _write_runtime_file(_MARKER_TARGET, _read_source(_MARKER_SOURCE))
        os.environ[_MARKER_PATH_VARIABLE] = str(_MARKER_TARGET)


def main() -> None:
    _prepare_runtime_files()
    os.execv(sys.executable, [sys.executable, "gui.py", *sys.argv[1:]])


if __name__ == "__main__":
    main()
