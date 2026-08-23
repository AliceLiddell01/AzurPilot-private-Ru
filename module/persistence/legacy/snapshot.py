"""Согласованные Stage-owned копии legacy-источников для rehearsal."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

from module.persistence.legacy.reader import LegacySourceError

_SQLITE_PATHS = (
    "config/cl1_data.db",
    "config/azurstats_local.db",
    "log/cl1/cl1_data.db",
)
_FILE_PATHS = ("log/device_id.json", "log/azurstat_meowofficer_farming.csv")
_MAX_SQLITE_SIZE = 64 * 1024 * 1024
_MAX_JSON_SIZE = 8 * 1024 * 1024
_MAX_FILE_SIZES = {
    "log/device_id.json": 16_384,
    "log/azurstat_meowofficer_farming.csv": 2 * 1024 * 1024,
}
_MAX_LEGACY_ENTRIES = 10_000
_MAX_LEGACY_SOURCES = 2_048


def _digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            result.update(chunk)
    return result.hexdigest()


def _bounded(root: Path, path: Path) -> Path:
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise LegacySourceError("SNAPSHOT_PATH_ESCAPE")
    current = path
    while current != root:
        if current.is_symlink() or current.is_junction():
            raise LegacySourceError("SNAPSHOT_SYMLINK_FORBIDDEN")
        current = current.parent
    return resolved


def _copy_stable(source: Path, destination: Path) -> None:
    for _ in range(3):
        before = _digest(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        after = _digest(source)
        if before == after == _digest(destination):
            return
        destination.unlink(missing_ok=True)
    raise LegacySourceError("SOURCE_CHANGED_DURING_SNAPSHOT")


def create_consistent_snapshot(source_root: Path, destination_root: Path) -> None:
    """Создать копию без изменения originals; destination обязан быть пустым."""

    source_root = source_root.resolve(strict=True)
    destination_root = destination_root.resolve(strict=True)
    if not source_root.is_dir() or not destination_root.is_dir():
        raise LegacySourceError("SNAPSHOT_ROOT_INVALID")
    if source_root == destination_root or any(destination_root.iterdir()):
        raise LegacySourceError("SNAPSHOT_DESTINATION_NOT_EMPTY")

    for relative in _SQLITE_PATHS:
        source = source_root / relative
        if not source.exists():
            continue
        source = _bounded(source_root, source)
        if source.stat().st_size > _MAX_SQLITE_SIZE:
            raise LegacySourceError("SNAPSHOT_SQLITE_TOO_LARGE")
        destination = destination_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        uri = f"file:{quote(source.as_posix(), safe='/:')}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as original:
            original.execute("PRAGMA query_only=ON")
            with closing(sqlite3.connect(destination)) as copy:
                original.backup(copy)
        with closing(
            sqlite3.connect(
                f"file:{quote(destination.as_posix(), safe='/:')}?mode=ro&immutable=1",
                uri=True,
            )
        ) as check:
            if check.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise LegacySourceError("SNAPSHOT_INTEGRITY_FAILED")

    legacy_root = source_root / "log" / "cl1"
    if legacy_root.exists():
        legacy_root = _bounded(source_root, legacy_root)
        candidates: list[Path] = []
        for index, source in enumerate(legacy_root.rglob("*"), start=1):
            if index > _MAX_LEGACY_ENTRIES:
                raise LegacySourceError("SNAPSHOT_SOURCE_COUNT_EXCEEDED")
            if source.is_file() and source.name in {
                "cl1_monthly.json",
                "cl1_monthly.json.bak",
            }:
                candidates.append(source)
                if len(candidates) > _MAX_LEGACY_SOURCES:
                    raise LegacySourceError("SNAPSHOT_SOURCE_COUNT_EXCEEDED")
        for source in sorted(candidates):
            safe = _bounded(source_root, source)
            if safe.stat().st_size > _MAX_JSON_SIZE:
                raise LegacySourceError("SNAPSHOT_JSON_TOO_LARGE")
            _copy_stable(safe, destination_root / safe.relative_to(source_root))

    for relative in _FILE_PATHS:
        source = source_root / relative
        if source.exists():
            safe = _bounded(source_root, source)
            if safe.stat().st_size > _MAX_FILE_SIZES[relative]:
                raise LegacySourceError("SNAPSHOT_FILE_TOO_LARGE")
            _copy_stable(safe, destination_root / relative)
