"""Общие bounded-примитивы offline legacy adapters."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

MAX_SQLITE_SIZE = 64 * 1024 * 1024
MAX_JSON_SIZE = 8 * 1024 * 1024
MAX_CSV_SIZE = 2 * 1024 * 1024
MAX_LEGACY_ENTRIES = 10_000
MAX_LEGACY_SOURCES = 2_048


class LegacySourceError(ValueError):
    """Источник нарушает bounded/read-only migration contract."""


def digest_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def bounded_path(
    root: Path,
    path: Path,
    *,
    escape_code: str,
    link_code: str,
) -> Path:
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise LegacySourceError(escape_code)
    current = path
    while current != root:
        if current.is_symlink() or current.is_junction():
            raise LegacySourceError(link_code)
        parent = current.parent
        if parent == current:
            raise LegacySourceError(escape_code)
        current = parent
    return resolved
