"""Адаптер логической резервной копии PostgreSQL для владельца Python Update."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from secrets import token_hex

from .config import DeploySettings
from .contracts import PostgreSqlBackupEvidence, ResultCode
from .errors import ToolingError
from .filesystem import (
    ScopedPath,
    StateLayout,
    is_unsafe_path,
    path_has_link,
    path_identity,
    sha256_file,
)


@dataclass(frozen=True)
class BackupOutcome:
    evidence: PostgreSqlBackupEvidence
    path: Path


class PostgreSqlBackupService:
    """Создать внешнюю проверенную резервную копию ``pg_dump -Fc`` без секретов в argv."""

    def create(
        self,
        root: Path,
        layout: StateLayout,
        settings: DeploySettings,
        *,
        pre_head: str,
        operation_id: str,
    ) -> BackupOutcome:
        backup_root = self._resolve_root(root, layout, settings.postgres_backup_root)
        backup_id = f"{operation_id}-postgres-{token_hex(6)}"
        directory = backup_root / backup_id
        if is_unsafe_path(backup_root) or is_unsafe_path(directory):
            raise ToolingError(
                ResultCode.TOOLING_BACKUP_REQUIRED,
                "Внешний каталог резервной копии PostgreSQL содержит небезопасный объект.",
            )
        try:
            directory.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            raise ToolingError(
                ResultCode.TOOLING_BACKUP_REQUIRED,
                "Не удалось создать внешний каталог резервной копии PostgreSQL.",
            ) from exc
        if is_unsafe_path(directory):
            raise ToolingError(
                ResultCode.TOOLING_BACKUP_REQUIRED,
                "Внешний каталог резервной копии PostgreSQL стал небезопасным.",
            )
        dump_path = directory / "database.dump"
        try:
            from dev_tools.postgresql_runtime import backup_for_repository

            backup_for_repository(root, dump_path, transport="docker")
            if (
                not dump_path.is_file()
                or is_unsafe_path(dump_path)
                or dump_path.stat().st_size < 1024
            ):
                raise RuntimeError("Файл логической резервной копии отсутствует или слишком мал.")
            digest = sha256_file(dump_path)
            evidence = PostgreSqlBackupEvidence(
                backup_id=backup_id,
                format="custom",
                validated=True,
                external=not dump_path.resolve().is_relative_to(root.resolve()),
                sha256=digest,
                pre_head=pre_head,
                provenance=path_identity(root),
            )
            ScopedPath(directory).atomic_write_text(
                "provenance.json",
                json.dumps(
                    evidence.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                ),
            )
            return BackupOutcome(evidence=evidence, path=dump_path)
        except ToolingError:
            raise
        except Exception as exc:
            raise ToolingError(
                ResultCode.TOOLING_BACKUP_FAILED,
                "Логическая резервная копия PostgreSQL не создана или не прошла проверку; изменения Git не выполнялись.",
            ) from exc

    @staticmethod
    def _resolve_root(
        root: Path,
        layout: StateLayout,
        configured: str | None,
    ) -> Path:
        if configured:
            raw = Path(configured).expanduser()
            if not raw.is_absolute():
                raw = root / raw
            if path_has_link(raw) or path_has_link(raw.parent):
                raise ToolingError(
                    ResultCode.TOOLING_BACKUP_REQUIRED,
                    "Путь резервной копии PostgreSQL содержит symlink или reparse point.",
                )
            destination = raw.resolve(strict=False)
        else:
            destination = layout.backups_directory / "postgresql"
        root = root.resolve(strict=True)
        if destination == root or destination.is_relative_to(root):
            raise ToolingError(
                ResultCode.TOOLING_BACKUP_REQUIRED,
                "Каталог резервной копии PostgreSQL должен находиться вне изменяемого checkout.",
            )
        if (
            path_has_link(destination)
            or is_unsafe_path(destination)
            or is_unsafe_path(destination.parent)
        ):
            raise ToolingError(
                ResultCode.TOOLING_BACKUP_REQUIRED,
                "Каталог резервной копии PostgreSQL содержит symlink или reparse point.",
            )
        try:
            destination.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ToolingError(
                ResultCode.TOOLING_BACKUP_REQUIRED,
                "Не удалось подготовить внешний каталог резервной копии PostgreSQL.",
            ) from exc
        if not destination.is_dir() or is_unsafe_path(destination):
            raise ToolingError(
                ResultCode.TOOLING_BACKUP_REQUIRED,
                "Каталог резервной копии PostgreSQL не является безопасным каталогом.",
            )
        return destination


__all__ = ["BackupOutcome", "PostgreSqlBackupService"]
