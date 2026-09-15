"""Безопасные файловые примитивы и внешнее хранилище журнала транзакций."""

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
MAX_TRANSACTION_ENTRIES = 128
TERMINAL_TRANSACTION_RETENTION = 32


def canonical_path(path: str | os.PathLike[str]) -> Path:
    """Вернуть канонический путь без доверия к текущему рабочему каталогу."""

    return Path(path).expanduser().resolve(strict=False)


def path_identity(path: Path) -> str:
    """Стабильный неперсональный идентификатор пути для доказательств и состояния."""

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


def _is_within(candidate: Path, parent: Path) -> bool:
    try:
        candidate.relative_to(parent)
        return True
    except ValueError:
        return False


class ScopedPath:
    """Проверить, что операция остаётся в разрешённой области без symlink."""

    def __init__(self, root: Path) -> None:
        self.root = canonical_path(root)
        if not self.root.is_dir():
            raise ToolingError(
                ResultCode.TOOLING_REPOSITORY_INVALID,
                "Корень области не является каталогом.",
            )

    def resolve(
        self, candidate: str | os.PathLike[str], *, allow_missing: bool = True
    ) -> Path:
        raw = Path(candidate)
        raw_absolute = Path(os.path.abspath(raw if raw.is_absolute() else self.root / raw))
        if _contains_link(raw_absolute):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Symlink, junction или reparse point запрещён в файловой области.",
            )
        resolved = canonical_path(raw_absolute)
        if not _is_within(resolved, self.root):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Файловая операция вышла за разрешённую область.",
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
                "Symlink, junction или reparse point запрещён в файловой области.",
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
    def safe_base(value: Path) -> Path:
        if path_has_link(value):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Корень состояния содержит symlink или reparse point.",
            )
        return canonical_path(value)

    configured = os.environ.get("AZURPILOT_STATE_HOME")
    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "AZURPILOT_STATE_HOME должен быть абсолютным путём.",
            )
        return safe_base(candidate)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("PROGRAMDATA")
        if base:
            return safe_base(Path(base) / "AzurPilot")
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return safe_base(Path(xdg) / "azurpilot")
    home = Path.home()
    if home:
        return safe_base(home / ".local" / "state" / "azurpilot")
    return safe_base(Path(tempfile.gettempdir()) / "azurpilot-state")


@dataclass(frozen=True)
class StateLayout:
    """Внешние блокировки, состояние и транзакции для одной идентичности репозитория."""

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
                "Корень состояния не может пересекаться с корнем репозитория.",
            )
        return cls(root, base, path_identity(root)[:24])

    @property
    def repository_directory(self) -> Path:
        return self.base / self.repository_id

    @property
    def transactions_directory(self) -> Path:
        return self.repository_directory / "transactions"

    @property
    def backups_directory(self) -> Path:
        return self.repository_directory / "backups"

    @property
    def bootstrap_cache_directory(self) -> Path:
        return self.repository_directory / "bootstrap-cache"

    def path(self, name: str) -> Path:
        if not name or Path(name).name != name:
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED, "Недопустимое имя файла состояния."
            )
        return self.repository_directory / name

    def ensure(self) -> Path:
        if _contains_link(self.base):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Корень состояния содержит symlink или reparse point.",
            )
        directories = (
            self.repository_directory,
            self.transactions_directory,
            self.backups_directory,
            self.bootstrap_cache_directory,
        )
        for directory in directories:
            if path_has_link(directory) or (
                directory.exists() and not directory.is_dir()
            ):
                raise ToolingError(
                    ResultCode.TOOLING_PRECONDITION_FAILED,
                    "Каталог состояния имеет небезопасный тип.",
                )
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
        for directory in directories:
            if _is_reparse_or_symlink(directory) or not directory.is_dir():
                raise ToolingError(
                    ResultCode.TOOLING_PRECONDITION_FAILED,
                    "Каталог состояния имеет небезопасный тип.",
                )
        return self.repository_directory


TModel = TypeVar("TModel", bound=BaseModel)


class JournalStore:
    """Атомарное хранение и fail-closed чтение внешнего журнала."""

    def __init__(self, layout: StateLayout, operation: str) -> None:
        if not operation or "/" in operation or "\\" in operation:
            raise ValueError("Операция должна быть коротким идентификатором")
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
                ResultCode.TOOLING_PRECONDITION_FAILED, "Недопустимый идентификатор транзакции."
            )
        return self.layout.transactions_directory / transaction_id

    def _validate_journal_paths(
        self, journal: TransactionJournal, directory: Path
    ) -> None:
        expected_venv = canonical_path(self.layout.repository_root / ".venv")
        expected_paths = {
            "candidate_path": canonical_path(directory / "candidate"),
            "backup_path": canonical_path(directory / "venv-backup"),
            "previous_path": canonical_path(directory / "venv-previous"),
        }
        if journal.venv_path is not None:
            raw_venv = Path(journal.venv_path).expanduser()
            if (
                not raw_venv.is_absolute()
                or path_has_link(raw_venv)
                or canonical_path(raw_venv) != expected_venv
            ):
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "Журнал транзакции указывает на неожиданный путь `.venv`.",
                )
        for field, expected in expected_paths.items():
            value = getattr(journal, field)
            if value is None:
                continue
            raw_value = Path(value).expanduser()
            if (
                not raw_value.is_absolute()
                or path_has_link(raw_value)
                or canonical_path(raw_value) != expected
            ):
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "Журнал транзакции содержит неожиданный файловый путь.",
                )

    def save(self, journal: TransactionJournal) -> Path:
        if journal.operation != self.operation:
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Журнал относится к другой операции.",
            )
        if journal.root_identity != path_identity(self.layout.repository_root):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Журнал относится к другому корню репозитория.",
            )
        self.layout.ensure()
        directory = self._directory(journal.transaction_id)
        self._validate_journal_paths(journal, directory)
        if directory.exists() and _is_reparse_or_symlink(directory):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Каталог транзакции содержит symlink или reparse point.",
            )
        directory.mkdir(parents=True, exist_ok=True)
        scope = ScopedPath(directory)
        updated = journal.model_copy(
            update={"updated_at": datetime.now(UTC).isoformat(timespec="seconds")}
        )
        data = updated.model_dump_json(indent=2).encode("utf-8")
        return scope.atomic_write_bytes("journal.json", data)

    def _read_entries(self) -> list[tuple[Path, TransactionJournal]]:
        root = self.layout.transactions_directory
        if _is_reparse_or_symlink(root):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Корень транзакции имеет небезопасный тип.",
            )
        if not root.exists():
            return []
        if _is_reparse_or_symlink(root) or not root.is_dir():
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Корень транзакции имеет небезопасный тип.",
            )
        try:
            entries = sorted(root.iterdir())
        except OSError as exc:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Не удалось просмотреть состояние транзакции.",
            ) from exc
        if len(entries) > MAX_TRANSACTION_ENTRIES:
            raise ToolingError(
                ResultCode.TOOLING_OPERATION_CONFLICT,
                "Число каталогов транзакций превышает безопасный предел сканирования.",
            )
        if any(not item.is_dir() or _is_reparse_or_symlink(item) for item in entries):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Корень транзакции содержит неожиданный или небезопасный объект.",
            )
        journals: list[tuple[Path, TransactionJournal]] = []
        for directory in entries:
            journal_path = directory / "journal.json"
            if not journal_path.exists():
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "Обнаружен каталог транзакции без журнала; продолжение запрещено.",
                )
            try:
                raw = bounded_read_text(journal_path, max_bytes=MAX_JOURNAL_BYTES)
                journal = TransactionJournal.model_validate_json(raw)
            except Exception as exc:
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "Журнал транзакции повреждён или имеет неизвестную схему.",
                ) from exc
            if (
                directory.name != journal.transaction_id
                or journal.operation not in {"build", "repair", "update"}
            ):
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "Журнал транзакции не соответствует своему каталогу или операции.",
                )
            self._validate_journal_paths(journal, directory)
            if journal.root_identity != path_identity(self.layout.repository_root):
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "Журнал транзакции относится к другому корню репозитория.",
                )
            if journal.phase in {"completed", "rolled_back"}:
                journals.append((directory, journal))
                continue
            if journal.phase not in {
                "initialized",
                "candidate_validated",
                "backup_ready",
                "environment_synchronized",
                "environment_built",
                "adb_ready",
                "shortcut_ready",
                "path_registered",
                "rebuild_started",
                "merge_pending",
                "merged",
                "merge_failed",
                "dependency_failed",
                "verification_failed",
                "rollback_unknown",
            }:
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "Журнал транзакции имеет неизвестную фазу; продолжение запрещено.",
                )
            journals.append((directory, journal))
        return journals

    def active(self) -> TransactionJournal | None:
        journals = self._read_entries()
        active = [
            journal
            for _, journal in journals
            if journal.operation == self.operation
            and journal.phase not in {"completed", "rolled_back"}
        ]
        if len(active) > 1:
            raise ToolingError(
                ResultCode.TOOLING_OPERATION_CONFLICT,
                "Обнаружено несколько незавершённых транзакций.",
            )
        return active[0] if active else None

    def terminal(self) -> tuple[tuple[Path, TransactionJournal], ...]:
        """Вернуть историю завершённых операций после строгой проверки всех записей состояния."""

        return tuple(
            (directory, journal)
            for directory, journal in self._read_entries()
            if journal.operation == self.operation
            and journal.phase in {"completed", "rolled_back"}
        )

    def retain_terminal(self, limit: int = TERMINAL_TRANSACTION_RETENTION) -> None:
        """Удалить только старые принадлежащие журналы завершённых операций после полной проверки."""

        if not 1 <= limit <= TERMINAL_TRANSACTION_RETENTION:
            raise ValueError("Срок хранения завершённых операций должен быть от 1 до 32")
        history = sorted(
            self.terminal(),
            key=lambda item: item[1].updated_at,
            reverse=True,
        )
        for directory, journal in history[limit:]:
            if journal.root_identity != path_identity(self.layout.repository_root):
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "Удаление истории транзакции не подтверждает владение.",
                )
            self.remove_owned(journal.transaction_id)

    def remove_owned(self, transaction_id: str) -> None:
        directory = self._directory(transaction_id)
        if not directory.exists() or _is_reparse_or_symlink(directory):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Удаление области транзакции не подтверждено.",
            )
        journal_path = directory / "journal.json"
        try:
            journal = TransactionJournal.model_validate_json(
                bounded_read_text(journal_path, max_bytes=MAX_JOURNAL_BYTES)
            )
        except Exception as exc:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Удаление области транзакции требует исправного журнала.",
            ) from exc
        if (
            journal.operation != self.operation
            or journal.transaction_id != transaction_id
            or directory.name != journal.transaction_id
            or journal.root_identity != path_identity(self.layout.repository_root)
            or journal.phase not in {"completed", "rolled_back"}
        ):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Удаление разрешено только для принадлежащей завершённой транзакции.",
            )
        self._validate_journal_paths(journal, directory)
        try:
            for item in directory.rglob("*"):
                if _is_reparse_or_symlink(item):
                    raise ToolingError(
                        ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                        "Область транзакции содержит symlink или reparse point.",
                    )
        except OSError as exc:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Не удалось проверить область транзакции перед удалением.",
            ) from exc
        quarantine = self.layout.repository_directory / (
            f".transaction-quarantine-{secrets.token_hex(16)}"
        )
        if os.path.lexists(str(quarantine)):
            raise ToolingError(
                ResultCode.TOOLING_OPERATION_CONFLICT,
                "Область карантина транзакции уже занята; удаление остановлено.",
            )
        try:
            os.replace(directory, quarantine)
        except OSError as exc:
            raise ToolingError(
                ResultCode.TOOLING_ROLLBACK_UNKNOWN,
                "Не удалось изолировать область транзакции перед удалением.",
            ) from exc
        try:
            shutil.rmtree(quarantine)
        except OSError as exc:
            raise ToolingError(
                ResultCode.TOOLING_ROLLBACK_UNKNOWN,
                "Удаление изолированной области транзакции не подтверждено.",
            ) from exc


def json_bytes(model: BaseModel) -> bytes:
    """Сериализовать модель ограниченным JSON без произвольных полей."""

    data = model.model_dump_json().encode("utf-8")
    if len(data) > MAX_JOURNAL_BYTES:
        raise ToolingError(
            ResultCode.TOOLING_PRECONDITION_FAILED,
            "JSON-результат превышает допустимый размер.",
        )
    return data


__all__ = [
    "MAX_FILE_BYTES",
    "MAX_TRANSACTION_ENTRIES",
    "TERMINAL_TRANSACTION_RETENTION",
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
