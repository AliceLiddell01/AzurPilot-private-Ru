"""Безопасные файловые примитивы и внешнее хранилище transaction journal."""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from .contracts import ResultCode, TransactionJournal
from .errors import ToolingError

MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_JOURNAL_BYTES = 256 * 1024


def canonical_path(path: str | os.PathLike[str]) -> Path:
    """Вернуть canonical path без доверия к текущему working directory."""

    return Path(path).expanduser().resolve(strict=False)


def path_identity(path: Path) -> str:
    """Стабильный неперсональный идентификатор пути для evidence/state."""

    value = os.path.normcase(str(canonical_path(path))).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


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
    """Проверить исходный путь до resolve, чтобы symlink не исчез из evidence."""

    current = Path(os.path.abspath(path))
    while True:
        if _is_reparse_or_symlink(current):
            return True
        if current.parent == current:
            return False
        current = current.parent


def is_unsafe_path(path: str | os.PathLike[str]) -> bool:
    """Проверить один filesystem object на symlink/reparse point."""

    return _is_reparse_or_symlink(Path(path))


def path_has_link(path: str | os.PathLike[str]) -> bool:
    """Проверить путь и его существующих предков на link-like object."""

    return _contains_link(Path(path))


def _is_within(candidate: Path, parent: Path) -> bool:
    try:
        candidate.relative_to(parent)
        return True
    except ValueError:
        return False


class ScopedPath:
    """Проверка, что операция остаётся в разрешённом non-symlink scope."""

    def __init__(self, root: Path) -> None:
        self.root = canonical_path(root)
        if not self.root.is_dir():
            raise ToolingError(
                ResultCode.TOOLING_REPOSITORY_INVALID,
                "Scope root не является каталогом.",
            )

    def resolve(
        self, candidate: str | os.PathLike[str], *, allow_missing: bool = True
    ) -> Path:
        raw = Path(candidate)
        raw_absolute = Path(os.path.abspath(raw if raw.is_absolute() else self.root / raw))
        if _contains_link(raw_absolute):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Symlink, junction или reparse point запрещён в файловом scope.",
            )
        resolved = canonical_path(raw_absolute)
        if not _is_within(resolved, self.root):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Файловая операция вышла за разрешённый scope.",
            )
        current = resolved
        missing: list[Path] = []
        while not current.exists() and current != self.root:
            missing.append(current)
            current = current.parent
        if current != self.root and not current.exists() and not allow_missing:
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Целевой путь не существует.",
            )
        if _is_reparse_or_symlink(current) or any(
            _is_reparse_or_symlink(item) for item in missing
        ):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Symlink, junction или reparse point запрещён в файловом scope.",
            )
        if not allow_missing and not resolved.exists():
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED, "Целевой путь не существует."
            )
        return resolved

    def ensure_directory(self, candidate: str | os.PathLike[str]) -> Path:
        resolved = self.resolve(candidate)
        resolved.mkdir(parents=True, exist_ok=True)
        if _is_reparse_or_symlink(resolved):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Созданный каталог оказался reparse point.",
            )
        return resolved

    def atomic_write_bytes(
        self, candidate: str | os.PathLike[str], data: bytes
    ) -> Path:
        if len(data) > MAX_FILE_BYTES:
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Записываемый файл превышает допустимый размер.",
            )
        destination = self.resolve(candidate)
        self.ensure_directory(destination.parent)
        if _is_reparse_or_symlink(destination):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED, "Перезапись symlink запрещена."
            )
        temp_path = destination.with_name(
            f".{destination.name}.{secrets.token_hex(8)}.tmp"
        )
        try:
            with temp_path.open("wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, destination)
        finally:
            if temp_path.exists():
                temp_path.unlink()
        return destination

    def atomic_write_text(self, candidate: str | os.PathLike[str], text: str) -> Path:
        return self.atomic_write_bytes(candidate, text.encode("utf-8"))


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


def sha256_file(path: Path) -> str:
    raw = Path(path)
    if _contains_link(raw):
        raise ToolingError(
            ResultCode.TOOLING_PRECONDITION_FAILED,
            "Хеширование symlink/reparse point запрещено.",
        )
    resolved = canonical_path(raw)
    digest = hashlib.sha256()
    try:
        with resolved.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise ToolingError(
            ResultCode.TOOLING_PRECONDITION_FAILED,
            "Не удалось вычислить SHA-256 файла.",
        ) from exc
    return digest.hexdigest()


def _default_state_base() -> Path:
    configured = os.environ.get("AZURPILOT_STATE_HOME")
    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "AZURPILOT_STATE_HOME должен быть абсолютным путём.",
            )
        return canonical_path(candidate)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("PROGRAMDATA")
        if base:
            return canonical_path(Path(base) / "AzurPilot")
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return canonical_path(Path(xdg) / "azurpilot")
    home = Path.home()
    if home:
        return canonical_path(home / ".local" / "state" / "azurpilot")
    return canonical_path(Path(tempfile.gettempdir()) / "azurpilot-state")


@dataclass(frozen=True)
class StateLayout:
    """Внешние locks/state/transactions для одной repository identity."""

    repository_root: Path
    base: Path
    repository_id: str

    @classmethod
    def for_repository(cls, repository_root: Path) -> StateLayout:
        root = canonical_path(repository_root)
        base = _default_state_base()
        if _is_within(base, root) or _is_within(root, base):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "State root не может пересекаться с repository root.",
            )
        return cls(root, base, path_identity(root)[:24])

    @property
    def repository_directory(self) -> Path:
        return self.base / self.repository_id

    @property
    def transactions_directory(self) -> Path:
        return self.repository_directory / "transactions"

    def path(self, name: str) -> Path:
        if not name or Path(name).name != name:
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED, "Недопустимое имя state-файла."
            )
        return self.repository_directory / name

    def ensure(self) -> Path:
        if _contains_link(self.base):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "State root содержит symlink или reparse point.",
            )
        self.transactions_directory.mkdir(parents=True, exist_ok=True)
        for directory in (self.repository_directory, self.transactions_directory):
            if _is_reparse_or_symlink(directory) or not directory.is_dir():
                raise ToolingError(
                    ResultCode.TOOLING_PRECONDITION_FAILED,
                    "State directory имеет небезопасный тип.",
                )
        return self.repository_directory


TModel = TypeVar("TModel", bound=BaseModel)


class JournalStore:
    """Атомарное хранение и fail-closed чтение внешнего журнала."""

    def __init__(self, layout: StateLayout, operation: str) -> None:
        if not operation or "/" in operation or "\\" in operation:
            raise ValueError("operation должен быть коротким идентификатором")
        self.layout = layout
        self.operation = operation

    def create(
        self, *, pre_head: str | None = None, target_head: str | None = None
    ) -> TransactionJournal:
        transaction_id = f"{self.operation}-{secrets.token_hex(12)}"
        return TransactionJournal(
            transaction_id=transaction_id,
            operation=self.operation,
            root_identity=path_identity(self.layout.repository_root),
            phase="initialized",
            pre_head=pre_head,
            target_head=target_head,
            updated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )

    def _directory(self, transaction_id: str) -> Path:
        if Path(transaction_id).name != transaction_id or len(transaction_id) > 80:
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED, "Недопустимый transaction id."
            )
        return self.layout.transactions_directory / transaction_id

    def save(self, journal: TransactionJournal) -> Path:
        if journal.operation != self.operation:
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Журнал относится к другой операции.",
            )
        self.layout.ensure()
        directory = self._directory(journal.transaction_id)
        if directory.exists() and _is_reparse_or_symlink(directory):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Transaction directory содержит symlink или reparse point.",
            )
        directory.mkdir(parents=True, exist_ok=True)
        scope = ScopedPath(directory)
        updated = journal.model_copy(
            update={"updated_at": datetime.now(UTC).isoformat(timespec="seconds")}
        )
        data = updated.model_dump_json(indent=2).encode("utf-8")
        return scope.atomic_write_bytes("journal.json", data)

    def active(self) -> TransactionJournal | None:
        root = self.layout.transactions_directory
        if _is_reparse_or_symlink(root):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Transaction root имеет небезопасный тип.",
            )
        if not root.exists():
            return None
        if _is_reparse_or_symlink(root) or not root.is_dir():
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Transaction root имеет небезопасный тип.",
            )
        try:
            entries = sorted(root.iterdir())
        except OSError as exc:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Не удалось просмотреть transaction state.",
            ) from exc
        if any(not item.is_dir() or _is_reparse_or_symlink(item) for item in entries):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Transaction root содержит неожиданный или небезопасный объект.",
            )
        children = entries
        if len(children) > 32:
            raise ToolingError(
                ResultCode.TOOLING_OPERATION_CONFLICT,
                "Число transaction-каталогов превышает лимит.",
            )
        active: list[TransactionJournal] = []
        for directory in children:
            journal_path = directory / "journal.json"
            if not journal_path.exists():
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "Обнаружен transaction-каталог без журнала; продолжение запрещено.",
                )
            try:
                raw = bounded_read_text(journal_path, max_bytes=MAX_JOURNAL_BYTES)
                journal = TransactionJournal.model_validate_json(raw)
            except Exception as exc:
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "Transaction journal повреждён или имеет неизвестную схему.",
                ) from exc
            if journal.operation != self.operation:
                continue
            if journal.phase not in {"completed", "rolled_back"}:
                active.append(journal)
        if len(active) > 1:
            raise ToolingError(
                ResultCode.TOOLING_OPERATION_CONFLICT,
                "Обнаружено несколько незавершённых транзакций.",
            )
        return active[0] if active else None

    def remove_owned(self, transaction_id: str) -> None:
        directory = self._directory(transaction_id)
        if not directory.exists() or _is_reparse_or_symlink(directory):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Удаление transaction scope не подтверждено.",
            )
        shutil.rmtree(directory)


def json_bytes(model: BaseModel) -> bytes:
    """Сериализовать модель ограниченным JSON без произвольных полей."""

    data = model.model_dump_json().encode("utf-8")
    if len(data) > MAX_JOURNAL_BYTES:
        raise ToolingError(
            ResultCode.TOOLING_PRECONDITION_FAILED,
            "JSON result превышает допустимый размер.",
        )
    return data


__all__ = [
    "MAX_FILE_BYTES",
    "JournalStore",
    "ScopedPath",
    "StateLayout",
    "bounded_read_bytes",
    "bounded_read_text",
    "canonical_path",
    "is_unsafe_path",
    "json_bytes",
    "path_has_link",
    "path_identity",
    "sha256_file",
]
