"""Минимальные безопасные файловые примитивы MCP process runtime."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from .mcp_errors import ToolingError
from .result import ResultCode

MAX_FILE_BYTES = 16 * 1024 * 1024

def canonical_path(path: str | os.PathLike[str]) -> Path:
    """Вернуть канонический путь без доверия к текущему рабочему каталогу."""

    return Path(path).expanduser().resolve(strict=False)

def _is_reparse_or_symlink(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(metadata.st_mode):
        return True
    reparse_flag = 0x400
    return bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag)


def _contains_link(path: Path) -> bool:
    """Проверить исходный путь до resolve, чтобы symlink не исчез из доказательств."""

    current = Path(os.path.abspath(path))
    while True:
        if _is_reparse_or_symlink(current):
            return True
        if current.parent == current:
            return False
        current = current.parent


def is_unsafe_path(path: str | os.PathLike[str]) -> bool:
    """Проверить один объект файловой системы на symlink/reparse point."""

    return _is_reparse_or_symlink(Path(path))


def path_has_link(path: str | os.PathLike[str]) -> bool:
    """Проверить путь и его существующих предков на link-like object."""

    return _contains_link(Path(path))

def bounded_read_bytes(path: Path, *, max_bytes: int = MAX_FILE_BYTES) -> bytes:
    raw = Path(path)
    if _contains_link(raw):
        raise ToolingError(
            ResultCode.TOOLING_PRECONDITION_FAILED,
            "Чтение symlink/reparse point запрещено.",
        )
    resolved = canonical_path(raw)
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        raise ToolingError(
            ResultCode.TOOLING_PRECONDITION_FAILED, "Не удалось получить размер файла."
        ) from exc
    if size > max_bytes:
        raise ToolingError(
            ResultCode.TOOLING_PRECONDITION_FAILED,
            "Файл превышает допустимый размер чтения.",
        )
    try:
        return resolved.read_bytes()
    except OSError as exc:
        raise ToolingError(
            ResultCode.TOOLING_PRECONDITION_FAILED, "Не удалось прочитать файл."
        ) from exc


def bounded_read_text(path: Path, *, max_bytes: int = MAX_FILE_BYTES) -> str:
    return bounded_read_bytes(path, max_bytes=max_bytes).decode(
        "utf-8-sig", errors="strict"
    )


__all__ = [
    "MAX_FILE_BYTES",
    "bounded_read_bytes",
    "bounded_read_text",
    "canonical_path",
    "is_unsafe_path",
    "path_has_link",
]
