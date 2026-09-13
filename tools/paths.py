"""Общие пути проекта для локальных инструментов и приёмочных запусков."""

from __future__ import annotations

from pathlib import Path


def _discover_repository_root(start: Path) -> Path:
    """Найти корень репозитория по устойчивым признакам проекта."""

    resolved = start.resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / "pyproject.toml").is_file() and (candidate / ".git").exists():
            return candidate
    raise RuntimeError(f"Не удалось определить корень репозитория от {resolved}.")


REPOSITORY_ROOT = _discover_repository_root(Path(__file__).parent)

__all__ = ["REPOSITORY_ROOT"]
