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
    if _ENV_SOURCE.exists() or _ENV_SOURCE.is_symlink():
        env_payload = _read_source(_ENV_SOURCE)
        if not (_PASSFILE_SOURCE.exists() and not _PASSFILE_SOURCE.is_symlink()):
            raise RuntimeError("PGPASSFILE среды выполнения Docker отсутствует или небезопасен.")
        _write_runtime_file(_PASSFILE_TARGET, _read_source(_PASSFILE_SOURCE))
        env_payload = _replace_passfile_paths(env_payload).replace(
            b"/run/secrets/azurpilot.pgpass",
            str(_PASSFILE_TARGET).encode("ascii"),
        )
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
