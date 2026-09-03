"""Ограниченное чтение бинарных файлов Dev Runtime."""

from __future__ import annotations

from pathlib import Path


class BoundedReadTooLarge(ValueError):
    """Файл нельзя безопасно буферизовать в установленном ограничении."""


def read_bounded_bytes(path: Path, *, max_bytes: int) -> bytes:
    """Прочитать не более ``max_bytes + 1`` байт и отклонить переполнение."""

    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("max_bytes должен быть неотрицательным целым числом")
    with path.open("rb") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise BoundedReadTooLarge
    return data


__all__ = ["BoundedReadTooLarge", "read_bounded_bytes"]
