"""Типизированный реестр владельца Bot Runtime и его worker-процессов."""

import errno
import json
import math
import os
import threading
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from deploy.atomic import atomic_remove, atomic_write

DEFAULT_WORKER_REGISTRY_FILE = Path("./config/state/bot-runtime/workers.json")
WORKER_REGISTRY_FILE = Path(
    os.environ.get("AZURPILOT_WORKER_REGISTRY_FILE", DEFAULT_WORKER_REGISTRY_FILE)
)
LEGACY_WORKER_REGISTRY_FILES = (
    Path("./cache/webui-workers.json"),
    Path("./config/webui-workers.json"),
)
LEGACY_WORKER_REGISTRY_FILE = LEGACY_WORKER_REGISTRY_FILES[0]
REGISTRY_LOCK_TIMEOUT = 10.0
REGISTRY_LOCK_RETRY_INTERVAL = 0.05
MAX_REGISTRY_BYTES = 128 * 1024

# Сначала сериализуем внутри одного процесса Python, чтобы не конкурировать повторно за системную файловую блокировку.
_registry_lock = threading.RLock()


class WorkerRegistryOwnershipError(RuntimeError):
    """Текущий процесс не владеет реестром Bot Runtime."""


class WorkerRegistryLockError(RuntimeError):
    """Не удалось получить блокировку реестра Bot Runtime в ограниченный срок."""


class ReadOnlyWorkerStatus(StrEnum):
    """Результат bounded read worker registry без lifecycle side effects."""

    VERIFIED = "verified"
    ABSENT = "absent"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ReadOnlyWorkerResult:
    """Типизированный результат чтения worker registry."""

    status: ReadOnlyWorkerStatus
    record: dict | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, ReadOnlyWorkerStatus):
            raise TypeError("status должен быть ReadOnlyWorkerStatus")
        if self.status is ReadOnlyWorkerStatus.VERIFIED:
            if not isinstance(self.record, dict):
                raise ValueError("Подтверждённый worker должен содержать запись")
        elif self.record is not None:
            raise ValueError("Неподтверждённый результат не должен содержать запись worker")


def _empty_registry(
    owner_pid: int | None = None,
    owner_created_at: float | None = None,
) -> dict:
    return {
        "owner_created_at": owner_created_at,
        "owner_pid": owner_pid,
        "workers": {},
    }


def _registry_lock_file(registry_file: Path | None = None) -> Path:
    """Вернуть путь межпроцессной блокировки рядом с реестром."""
    if registry_file is None:
        registry_file = WORKER_REGISTRY_FILE
    return registry_file.with_name(f"{registry_file.name}.lock")


def _legacy_registry_lock_file(registry_file: Path) -> Path:
    """Вернуть путь блокировки конкретного legacy-реестра."""
    return _registry_lock_file(registry_file)


def _prepare_lock_file(lock_file: Path):
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_file.open("a+b")
    try:
        # msvcrt.locking() не может заблокировать пустой файл, поэтому сохраняем один байт для блокировки.
        if lock_file.stat().st_size == 0:
            handle.seek(0)
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        return handle
    except Exception:
        handle.close()
        raise


def _is_lock_conflict(exc: OSError) -> bool:
    return (
        isinstance(exc, PermissionError)
        or exc.errno in (errno.EACCES, errno.EAGAIN)
        or getattr(exc, "winerror", None) in (32, 33)
    )


def _acquire_file_lock(handle) -> None:
    deadline = time.monotonic() + REGISTRY_LOCK_TIMEOUT

    if os.name == "nt":
        import msvcrt

        def acquire() -> None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    else:
        try:
            import fcntl
        except ImportError as exc:
            raise WorkerRegistryLockError("Эта платформа не поддерживает блокировку реестра рабочих процессов") from exc

        def acquire() -> None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    while True:
        try:
            acquire()
            return
        except OSError as exc:
            if not _is_lock_conflict(exc):
                raise WorkerRegistryLockError(f"Не удалось заблокировать реестр рабочих процессов: {exc}") from exc
            if time.monotonic() >= deadline:
                raise WorkerRegistryLockError("Истекло время ожидания блокировки реестра рабочих процессов") from exc
            time.sleep(REGISTRY_LOCK_RETRY_INTERVAL)


def _release_file_lock(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _locked_file(lock_file: Path) -> Iterator[None]:
    """Защитить runtime-файл межпроцессной блокировкой."""
    handle = _prepare_lock_file(lock_file)
    acquired = False
    try:
        _acquire_file_lock(handle)
        acquired = True
        yield
    finally:
        if acquired:
            _release_file_lock(handle)
        handle.close()


def _legacy_registry_enabled() -> bool:
    """Включить миграцию старых путей только для штатного реестра."""
    return WORKER_REGISTRY_FILE == DEFAULT_WORKER_REGISTRY_FILE


def _reject_registry_path_links(registry_file: Path) -> None:
    candidate = Path(os.path.abspath(registry_file))
    for path in (candidate, *candidate.parents):
        if path.is_symlink() or (
            hasattr(path, "is_junction") and path.is_junction()
        ):
            raise RuntimeError("Путь реестра рабочих процессов проходит через ссылку")


def _canonical_registry_file(repository_root: Path | str | None = None) -> Path:
    if repository_root is None:
        registry_file = WORKER_REGISTRY_FILE
    else:
        registry_file = Path(repository_root).resolve() / "config" / "state" / "bot-runtime" / "workers.json"
    _reject_registry_path_links(registry_file)
    return registry_file


def _record_is_alive(record: dict | None) -> bool | None:
    """Проверить точную идентичность процесса, сохраняя результат проверки."""
    if record is None:
        return False
    if "created_at" not in record:
        return _pid_exists(record["pid"])
    return process_matches(record)


def _terminate_proven_orphan_worker(record: dict, worker_name: str) -> None:
    """Ограниченно завершить только worker с подтверждённой PID/creation-time identity."""
    pid = record["pid"]
    try:
        matches = process_matches(record)
    except (OSError, RuntimeError) as exc:
        raise WorkerRegistryOwnershipError(
            f"Нельзя подтвердить orphan worker {worker_name} перед очисткой"
        ) from exc
    if matches is None:
        return
    if matches is False:
        return
    if os.name == "nt":
        try:
            import subprocess

            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkerRegistryOwnershipError(
                f"Не удалось ограниченно завершить orphan worker {worker_name}"
            ) from exc
    else:
        try:
            import psutil

            process = psutil.Process(pid)
            if abs(process.create_time() - float(record["created_at"])) >= 0.01:
                return
            children = process.children(recursive=True)
            for child in reversed(children):
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            process.kill()
            _, alive = psutil.wait_procs([*children, process], timeout=3)
            if alive:
                raise WorkerRegistryOwnershipError(
                    f"Orphan worker {worker_name} не завершился в ограниченный срок"
                )
        except WorkerRegistryOwnershipError:
            raise
        except Exception as exc:  # noqa: BLE001 - неизвестная identity блокирует takeover.
            try:
                import psutil

                if isinstance(exc, psutil.NoSuchProcess):
                    return
            except ImportError:
                pass
            raise WorkerRegistryOwnershipError(
                f"Не удалось ограниченно завершить orphan worker {worker_name}"
            ) from exc

    deadline = time.monotonic() + 3
    while True:
        try:
            matches = process_matches(record)
        except (OSError, RuntimeError) as exc:
            raise WorkerRegistryOwnershipError(
                f"Не удалось подтвердить завершение orphan worker {worker_name}"
            ) from exc
        if matches is None:
            return
        if matches is False:
            return
        if time.monotonic() >= deadline:
            raise WorkerRegistryOwnershipError(
                f"Orphan worker {worker_name} ещё работает после bounded cleanup"
            )
        time.sleep(0.05)


def _migrate_legacy_registry() -> Path:
    """Мигрировать старый реестр только после проверки owner и всех worker."""
    if not _legacy_registry_enabled():
        return WORKER_REGISTRY_FILE
    legacy_files = [path for path in LEGACY_WORKER_REGISTRY_FILES if os.path.lexists(path)]
    if not legacy_files:
        return WORKER_REGISTRY_FILE

    legacy_registries = [_read_registry(path) for path in legacy_files]
    if any(registry != legacy_registries[0] for registry in legacy_registries[1:]):
        raise WorkerRegistryLockError(
            "Legacy-реестры runtime расходятся; автоматическая миграция отклонена"
        )
    legacy_registry = legacy_registries[0]

    def owner_is_proven_dead(record: dict | None, label: str) -> bool:
        if record is None:
            return False
        pid = record.get("pid")
        created_at = record.get("created_at")
        if (
            isinstance(pid, bool)
            or not isinstance(pid, int)
            or pid <= 0
        ):
            raise WorkerRegistryOwnershipError(f"Идентичность {label} некорректна")
        if created_at is None:
            if _pid_exists(pid):
                raise WorkerRegistryOwnershipError(
                    f"{label} PID {pid} существует, но его точная identity неизвестна"
                )
            return True
        try:
            matches = process_matches(record)
        except (OSError, RuntimeError) as exc:
            raise WorkerRegistryOwnershipError(
                f"Идентичность {label} PID {pid} невозможно подтвердить"
            ) from exc
        if matches is True:
            raise WorkerRegistryOwnershipError(
                f"{label} PID {pid} ещё работает; передача ownership отклонена"
            )
        return True

    legacy_owner_dead = owner_is_proven_dead(
        _owner_record(legacy_registry), "прежнего владельца Bot Runtime"
    )
    for worker_name, worker in tuple(legacy_registry["workers"].items()):
        if not isinstance(worker, dict) or not _valid_worker_identity(worker):
            raise WorkerRegistryOwnershipError(
                f"Запись прежнего worker {worker_name} имеет неподтверждённый формат"
            )
        try:
            matches = _record_is_alive(worker)
        except (OSError, RuntimeError) as exc:
            raise WorkerRegistryOwnershipError(
                f"Нельзя подтвердить identity прежнего worker {worker_name}"
            ) from exc
        if matches is True:
            if not legacy_owner_dead:
                raise WorkerRegistryOwnershipError(
                    f"Живой worker {worker_name} нельзя отделить от неизвестного прежнего owner"
                )
            _terminate_proven_orphan_worker(worker, worker_name)

    if os.path.lexists(WORKER_REGISTRY_FILE):
        current_registry = _read_registry(WORKER_REGISTRY_FILE)
        current_owner = _owner_record(current_registry)
        current_owner_is_self = False
        if current_owner is not None:
            current_owner_is_self = (
                current_owner.get("pid") == os.getpid()
                and "created_at" in current_owner
                and process_matches(current_owner) is True
            )
            if not current_owner_is_self:
                current_owner_dead = owner_is_proven_dead(
                    current_owner, "canonical Bot Runtime owner"
                )
            else:
                current_owner_dead = False
        else:
            current_owner_dead = False
        if not current_owner_is_self:
            for worker_name, worker in tuple(current_registry["workers"].items()):
                if not isinstance(worker, dict) or not _valid_worker_identity(worker):
                    raise WorkerRegistryOwnershipError(
                        f"Запись canonical worker {worker_name} имеет неподтверждённый формат"
                    )
                try:
                    matches = _record_is_alive(worker)
                except (OSError, RuntimeError) as exc:
                    raise WorkerRegistryOwnershipError(
                        f"Нельзя подтвердить identity canonical worker {worker_name}"
                    ) from exc
                if matches is True:
                    if not current_owner_dead:
                        raise WorkerRegistryOwnershipError(
                            f"Живой canonical worker {worker_name} нельзя отделить от неизвестного owner"
                        )
                    _terminate_proven_orphan_worker(worker, worker_name)
            _write_registry(_empty_registry(), WORKER_REGISTRY_FILE)
    else:
        WORKER_REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _write_registry(_empty_registry(), WORKER_REGISTRY_FILE)

    for legacy_file in legacy_files:
        try:
            atomic_remove(legacy_file)
        except OSError as exc:
            raise RuntimeError(f"Не удалось удалить legacy-реестр worker: {exc}") from exc
    return WORKER_REGISTRY_FILE


@contextmanager
def _locked_registry(repository_root: Path | str | None = None) -> Iterator[Path]:
    """Защитить транзакцию реестра внутрипроцессной и системными блокировками."""
    with _registry_lock:
        legacy_root_matches = repository_root is None or (
            Path(repository_root).resolve() == Path.cwd().resolve()
        )
        if _legacy_registry_enabled() and legacy_root_matches:
            with ExitStack() as stack:
                for legacy_file in LEGACY_WORKER_REGISTRY_FILES:
                    stack.enter_context(
                        _locked_file(_legacy_registry_lock_file(legacy_file))
                    )
                stack.enter_context(_locked_file(_registry_lock_file()))
                yield _migrate_legacy_registry()
        else:
            registry_file = _canonical_registry_file(repository_root)
            with _locked_file(_registry_lock_file(registry_file)):
                yield registry_file


def _read_registry(registry_file: Path) -> dict:
    _reject_registry_path_links(registry_file)
    try:
        if registry_file.stat().st_size > MAX_REGISTRY_BYTES:
            raise RuntimeError("Реестр рабочих процессов превышает допустимый размер")
        raw = registry_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty_registry()
    except OSError as exc:
        raise RuntimeError(f"Не удалось прочитать реестр рабочих процессов: {exc}") from exc

    try:
        registry = json.loads(raw)
        if not isinstance(registry, dict):
            raise ValueError("Корень реестра должен быть объектом")
        if set(registry) != {"owner_pid", "owner_created_at", "workers"}:
            raise ValueError("Реестр содержит неизвестные поля")
        owner_pid = registry.get("owner_pid")
        owner_created_at = registry.get("owner_created_at")
        workers = registry.get("workers")
        if owner_pid is not None:
            if isinstance(owner_pid, bool) or not isinstance(owner_pid, int) or owner_pid <= 0:
                raise ValueError("owner_pid имеет неверный формат")
            if owner_created_at is not None:
                if (
                    isinstance(owner_created_at, bool)
                    or not isinstance(owner_created_at, (int, float))
                    or not math.isfinite(float(owner_created_at))
                    or float(owner_created_at) <= 0
                ):
                    raise ValueError("owner_created_at имеет неверный формат")
                owner_created_at = float(owner_created_at)
        else:
            if owner_created_at is not None:
                raise ValueError("owner_created_at задан без owner_pid")
            owner_created_at = None
        if not isinstance(workers, dict):
            raise ValueError("Поле workers должно быть объектом")
        if any(not isinstance(name, str) or not name for name in workers):
            raise ValueError("Имена workers должны быть непустыми строками")
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Недопустимый формат реестра рабочих процессов: {exc}") from exc

    return {
        "owner_created_at": owner_created_at,
        "owner_pid": owner_pid,
        "workers": workers,
    }


def _write_registry(registry: dict, registry_file: Path) -> None:
    atomic_write(
        registry_file,
        json.dumps(registry, ensure_ascii=True, sort_keys=True),
    )


def _process_created_at(pid: int) -> float:
    try:
        import psutil

        return psutil.Process(pid).create_time()
    except Exception as exc:
        raise RuntimeError(f"Не удалось получить время создания рабочего процесса PID {pid}: {exc}") from exc


def _owner_record(registry: dict) -> dict | None:
    owner_pid = registry["owner_pid"]
    if owner_pid is None:
        return None
    record = {"pid": owner_pid}
    if registry["owner_created_at"] is not None:
        record["created_at"] = registry["owner_created_at"]
    return record


def _require_current_owner(registry: dict, owner_pid: int) -> None:
    """Подтвердить, что PID вызова принадлежит текущему Bot Runtime owner."""
    try:
        expected_pid = int(owner_pid)
    except (TypeError, ValueError) as exc:
        raise WorkerRegistryOwnershipError(f"Недопустимый PID владельца Bot Runtime: {owner_pid}") from exc

    record = _owner_record(registry)
    if record is None or record["pid"] != expected_pid:
        raise WorkerRegistryOwnershipError(
            f"Владелец реестра рабочих процессов не совпадает: {registry['owner_pid']} != {expected_pid}"
        )
    if "created_at" not in record:
        raise WorkerRegistryOwnershipError("У владельца реестра нет данных идентификации процесса")

    try:
        created_at = _process_created_at(expected_pid)
    except RuntimeError as exc:
        raise WorkerRegistryOwnershipError(
            f"Не удалось проверить владельца Bot Runtime PID {expected_pid}: {exc}"
        ) from exc
    if abs(created_at - record["created_at"]) < 0.01:
        return
    raise WorkerRegistryOwnershipError(
        f"Идентификатор владельца Bot Runtime PID {expected_pid} не совпадает; изменение реестра отклонено"
    )


def is_current_owner(owner_pid: int) -> bool:
    """Проверить, принадлежит ли PID текущему Bot Runtime owner."""
    with _locked_registry() as registry_file:
        registry = _read_registry(registry_file)
        try:
            _require_current_owner(registry, owner_pid)
        except WorkerRegistryOwnershipError:
            return False
        return True


def _remove_proven_dead_workers(
    registry: dict,
    *,
    allow_orphan_cleanup: bool = False,
) -> None:
    """Удалить только мёртвые workers или bounded-cleanup при доказанно мёртвом owner."""
    for worker_name, worker in tuple(registry["workers"].items()):
        if not isinstance(worker, dict) or not _valid_worker_identity(worker):
            raise WorkerRegistryOwnershipError(
                f"Запись orphan worker {worker_name} имеет неподтверждённый формат"
            )
        try:
            worker_matches = process_matches(worker)
        except (OSError, RuntimeError) as exc:
            raise WorkerRegistryOwnershipError(
                f"Нельзя подтвердить identity orphan worker {worker_name}"
            ) from exc
        if worker_matches is True:
            if not allow_orphan_cleanup:
                raise WorkerRegistryOwnershipError(
                    f"Orphan worker {worker_name} ещё работает; передача ownership отклонена"
                )
            _terminate_proven_orphan_worker(worker, worker_name)
        registry["workers"].pop(worker_name, None)


def claim_owner(
    owner_pid: int,
    *,
    repository_root: Path | str | None = None,
) -> None:
    """Атомарно объявить текущий процесс единственным Bot Runtime owner."""
    try:
        owner_pid = int(owner_pid)
    except (TypeError, ValueError) as exc:
        raise WorkerRegistryOwnershipError(f"Недопустимый PID владельца Bot Runtime: {owner_pid}") from exc
    owner_created_at = _process_created_at(owner_pid)

    with _locked_registry(repository_root) as registry_file:
        registry = _read_registry(registry_file)
        previous_owner = _owner_record(registry)
        previous_owner_dead = False
        if previous_owner is not None:
            same_owner = (
                previous_owner["pid"] == owner_pid
                and previous_owner.get("created_at") is not None
                and abs(previous_owner["created_at"] - owner_created_at) < 0.01
            )
            if same_owner:
                # Повторная инициализация того же owner сохраняет зарегистрированные worker-процессы.
                return

            if "created_at" not in previous_owner:
                if _pid_exists(previous_owner["pid"]):
                    raise WorkerRegistryOwnershipError(
                        "У прежнего владельца Bot Runtime нет точной identity; перезапись реестра отклонена"
                    )
                previous_owner_dead = True
            else:
                try:
                    previous_owner_alive = process_matches(previous_owner)
                except (OSError, RuntimeError) as exc:
                    raise WorkerRegistryOwnershipError(
                        f"Не удалось проверить прежнего владельца Bot Runtime: {exc}"
                    ) from exc
                if previous_owner_alive is True:
                    raise WorkerRegistryOwnershipError(
                        f"Владелец Bot Runtime ещё работает (PID: {previous_owner['pid']}); запуск второго owner отклонён"
                    )
                previous_owner_dead = True

        _remove_proven_dead_workers(
            registry,
            allow_orphan_cleanup=previous_owner_dead,
        )

        _write_registry(_empty_registry(owner_pid, owner_created_at), registry_file)


def register_worker(owner_pid: int, config_name: str, pid: int) -> dict[str, float | int]:
    """Зарегистрировать worker, чтобы Bot Runtime мог подтвердить его identity."""
    try:
        pid = int(pid)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Недопустимый PID рабочего процесса: {pid}") from exc

    created_at = _process_created_at(pid)
    record = {"created_at": created_at, "pid": pid}
    with _locked_registry() as registry_file:
        registry = _read_registry(registry_file)
        _require_current_owner(registry, owner_pid)
        registry["workers"][config_name] = record
        _write_registry(registry, registry_file)
    return deepcopy(record)


def unregister_worker(
    owner_pid: int,
    config_name: str,
    *,
    expected_worker: dict | None,
) -> bool:
    """Удалить запись worker только при совпадении ожидаемой identity."""
    with _locked_registry() as registry_file:
        registry = _read_registry(registry_file)
        try:
            _require_current_owner(registry, owner_pid)
        except WorkerRegistryOwnershipError:
            return False
        current_worker = registry["workers"].get(config_name)
        if expected_worker is None:
            return False
        if not _same_worker_identity(current_worker, expected_worker):
            return False
        registry["workers"].pop(config_name, None)
        _write_registry(registry, registry_file)
        return True


def _same_worker_identity(left: object, right: object) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    if not _valid_worker_identity(left) or not _valid_worker_identity(right):
        return False
    return left["pid"] == right["pid"] and float(left["created_at"]) == float(
        right["created_at"]
    )


def _valid_worker_identity(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    try:
        pid = record["pid"]
        created_at = record["created_at"]
    except KeyError:
        return False
    try:
        if (
            isinstance(pid, bool)
            or not isinstance(pid, int)
            or pid <= 0
            or isinstance(created_at, bool)
            or not isinstance(created_at, (int, float))
            or not math.isfinite(float(created_at))
            or float(created_at) <= 0
        ):
            return False
    except (TypeError, ValueError, OverflowError):
        return False
    return True


def get_workers(
    owner_pid: int,
    *,
    repository_root: Path | str | None = None,
) -> dict[str, dict]:
    """Вернуть снимок workers, принадлежащих указанному runtime owner."""
    with _locked_registry(repository_root) as registry_file:
        registry = _read_registry(registry_file)
        if registry["owner_pid"] != owner_pid:
            return {}
        return deepcopy(registry["workers"])


def get_worker_read_only(config_name: str) -> dict | None:
    """Прочитать worker без миграции и блокировки, не проверяя владельца."""
    result = read_worker_read_only(config_name)
    if result.status is ReadOnlyWorkerStatus.UNKNOWN:
        raise RuntimeError("Не удалось подтвердить состояние реестра рабочих процессов")
    return deepcopy(result.record) if result.record is not None else None


def read_canonical_worker_read_only(
    config_name: str,
    *,
    repository_root: Path | str | None = None,
) -> ReadOnlyWorkerResult:
    """Прочитать только canonical worker registry без миграции и блокировки."""
    if not isinstance(config_name, str) or not config_name:
        raise ValueError("Имя экземпляра должно быть непустой строкой")
    registry_file = _canonical_registry_file(repository_root)
    if not os.path.lexists(registry_file):
        return ReadOnlyWorkerResult(ReadOnlyWorkerStatus.ABSENT)
    try:
        registry = _read_registry(registry_file)
    except RuntimeError:
        return ReadOnlyWorkerResult(ReadOnlyWorkerStatus.UNKNOWN)
    record = registry["workers"].get(config_name)
    if record is None:
        return ReadOnlyWorkerResult(ReadOnlyWorkerStatus.ABSENT)
    if not _valid_worker_identity(record):
        return ReadOnlyWorkerResult(ReadOnlyWorkerStatus.UNKNOWN)
    return ReadOnlyWorkerResult(
        ReadOnlyWorkerStatus.VERIFIED,
        record=deepcopy(record),
    )


def get_canonical_worker_read_only(
    config_name: str,
    *,
    repository_root: Path | str | None = None,
) -> dict | None:
    result = read_canonical_worker_read_only(
        config_name, repository_root=repository_root
    )
    if result.status is ReadOnlyWorkerStatus.UNKNOWN:
        raise RuntimeError("Не удалось подтвердить canonical worker registry")
    return deepcopy(result.record) if result.record is not None else None


def get_canonical_workers_read_only(
    *,
    repository_root: Path | str | None = None,
) -> dict[str, dict]:
    """Прочитать все worker canonical registry без миграции и побочных эффектов."""
    registry_file = _canonical_registry_file(repository_root)
    if not os.path.lexists(registry_file):
        return {}
    registry = _read_registry(registry_file)
    workers = registry["workers"]
    if any(not _valid_worker_identity(record) for record in workers.values()):
        raise RuntimeError("Canonical worker registry содержит неподтверждённую identity")
    return deepcopy(workers)


def read_worker_read_only(config_name: str) -> ReadOnlyWorkerResult:
    """Типизированно прочитать worker без миграции и блокировки."""
    if not isinstance(config_name, str) or not config_name:
        raise ValueError("Имя экземпляра должно быть непустой строкой")

    try:
        paths, legacy_registries = _read_only_registry_paths()
    except RuntimeError:
        return ReadOnlyWorkerResult(ReadOnlyWorkerStatus.UNKNOWN)
    unavailable = False
    for registry_file in paths:
        if not registry_file.is_file():
            continue
        if registry_file in legacy_registries:
            registry = legacy_registries[registry_file]
        else:
            try:
                registry = _read_registry(registry_file)
            except RuntimeError:
                unavailable = True
                continue
        workers = registry["workers"]
        if config_name not in workers:
            continue
        record = workers[config_name]
        if not _valid_worker_identity(record):
            return ReadOnlyWorkerResult(ReadOnlyWorkerStatus.UNKNOWN)
        return ReadOnlyWorkerResult(
            ReadOnlyWorkerStatus.VERIFIED,
            record=deepcopy(record),
        )

    status = ReadOnlyWorkerStatus.UNKNOWN if unavailable else ReadOnlyWorkerStatus.ABSENT
    return ReadOnlyWorkerResult(status)


def _read_only_registry_paths() -> tuple[tuple[Path, ...], dict[Path, dict]]:
    """Выбрать порядок чтения, не изменяя файлы и не создавая блокировки."""
    current_file = WORKER_REGISTRY_FILE
    if not _legacy_registry_enabled():
        return (current_file,), {}

    active_legacy: list[Path] = []
    inactive_legacy: list[Path] = []
    registries: dict[Path, dict] = {}
    for legacy_file in LEGACY_WORKER_REGISTRY_FILES:
        if legacy_file == current_file or not os.path.lexists(legacy_file):
            continue
        try:
            registry = _read_registry(legacy_file)
        except RuntimeError:
            raise RuntimeError("Legacy worker registry не прошёл проверку")
        registries[legacy_file] = registry
        owner = _owner_record(registry)
        if owner is None:
            if registry["workers"]:
                raise RuntimeError("Legacy worker registry не содержит identity owner")
            inactive_legacy.append(legacy_file)
            continue
        if "created_at" not in owner:
            raise RuntimeError("Legacy worker registry не содержит точную identity owner")
        try:
            owner_matches = process_matches(owner)
        except (OSError, RuntimeError) as exc:
            raise RuntimeError("Legacy worker registry owner identity неизвестна") from exc
        if owner_matches is True:
            active_legacy.append(legacy_file)
        else:
            inactive_legacy.append(legacy_file)

    legacy_values = list(registries.values())
    if legacy_values and any(value != legacy_values[0] for value in legacy_values[1:]):
        raise RuntimeError("Legacy worker registries расходятся")
    if os.path.lexists(current_file):
        canonical = _read_registry(current_file)
        if legacy_values and canonical != legacy_values[0]:
            raise RuntimeError("Canonical и legacy worker registries расходятся")
    return tuple((*active_legacy, current_file, *inactive_legacy)), registries


def _iter_read_only_registries() -> Iterator[dict]:
    """Перечислить доступные registry без миграции и создания lock-файлов."""

    paths, legacy_registries = _read_only_registry_paths()
    for registry_file in paths:
        if not registry_file.is_file():
            continue
        if registry_file in legacy_registries:
            registry = legacy_registries[registry_file]
        else:
            try:
                registry = _read_registry(registry_file)
            except RuntimeError:
                raise
        yield registry


def get_owner() -> int | None:
    """Вернуть PID текущего owner из authoritative registry."""
    with _locked_registry() as registry_file:
        return _read_registry(registry_file)["owner_pid"]


def get_owner_record(
    repository_root: Path | str | None = None,
) -> dict | None:
    """Вернуть PID и время создания процесса Bot Runtime owner."""
    with _locked_registry(repository_root) as registry_file:
        registry = _read_registry(registry_file)
        return _owner_record(registry)


def get_owner_record_read_only() -> dict | None:
    """Прочитать owner без миграции registry и создания lock-файлов."""
    for registry in _iter_read_only_registries():
        owner = _owner_record(registry)
        if owner is not None:
            return deepcopy(owner)
    return None


def get_canonical_owner_record_read_only(
    *,
    repository_root: Path | str | None = None,
) -> dict | None:
    """Прочитать только владельца Bot Runtime без legacy-путей и побочных эффектов."""
    registry_file = _canonical_registry_file(repository_root)
    if not os.path.lexists(registry_file):
        return None
    registry = _read_registry(registry_file)
    return _owner_record(registry)


def clear_owner(
    owner_pid: int,
    *,
    repository_root: Path | str | None = None,
) -> bool:
    """Очистить запись owner только после подтверждённой остановки его workers."""
    with _locked_registry(repository_root) as registry_file:
        registry = _read_registry(registry_file)
        record = _owner_record(registry)
        if record is None:
            return True
        if record["pid"] != owner_pid:
            raise WorkerRegistryOwnershipError(
                f"Владелец реестра рабочих процессов не совпадает: {record['pid']} != {owner_pid}"
            )

        if registry["workers"]:
            raise WorkerRegistryOwnershipError(
                "Нельзя освободить Bot Runtime owner, пока в реестре остались workers"
            )

        if owner_pid == os.getpid():
            _require_current_owner(registry, owner_pid)
        elif "created_at" not in record:
            if _pid_exists(owner_pid):
                raise WorkerRegistryOwnershipError(
                    f"Прежний владелец Bot Runtime PID {owner_pid} ещё существует; очистка реестра отклонена"
                )
        else:
            try:
                owner_matches = process_matches(record)
            except (OSError, RuntimeError) as exc:
                raise WorkerRegistryOwnershipError(
                    f"Не удалось проверить прежнего владельца Bot Runtime PID {owner_pid}: {exc}"
                ) from exc
            if owner_matches is True:
                raise WorkerRegistryOwnershipError(
                    f"Владелец Bot Runtime ещё работает (PID: {owner_pid}); очистка реестра отклонена"
                )

        _write_registry(_empty_registry(), registry_file)
        return True


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def process_matches(record: dict) -> bool | None:
    """Сверить PID и время создания; ``None`` означает, что процесс завершён."""
    try:
        pid = int(record["pid"])
        created_at = float(record["created_at"])
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("Недопустимая запись реестра рабочего процесса")

    try:
        import psutil

        process = psutil.Process(pid)
        if process.status() == psutil.STATUS_ZOMBIE:
            return None
        return abs(process.create_time() - created_at) < 0.01
    except Exception as exc:
        try:
            if not os.path.exists(f"/proc/{pid}") and os.name != "nt":
                return None
        except OSError:
            pass
        try:
            import psutil

            if isinstance(exc, psutil.NoSuchProcess):
                return None
        except ImportError:
            pass
        raise RuntimeError(f"Не удалось проверить рабочий процесс PID {pid}: {exc}") from exc
