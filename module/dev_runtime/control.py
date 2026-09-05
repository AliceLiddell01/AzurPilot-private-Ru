"""Постоянный control plane для назначенного development target.

Модуль намеренно не импортирует ``Device``. Read-only статус использует только
легковесный ADB probe, а mutating operations делегируют запуск и остановку
эмулятора существующему ``Platform`` и жизненный цикл приложения существующему
ADB backend ``AppControl``. Каждая длительная операция хранится в отдельном
repository-scoped marker и исполняется фиксированным supervisor-процессом.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from deploy.atomic import file_write, replace_tmp, to_tmp_file
from module.dev_runtime.bounded_io import BoundedReadTooLarge, read_bounded_bytes
from module.dev_runtime.contracts import DevEnvironment, DevResult
from module.dev_runtime.coordination import (
    RuntimeCoordinationError,
    runtime_coordination_lock,
)
from module.dev_runtime.target import (
    DevTarget,
    DevTargetError,
    DevTargetRegistry,
    target_identity,
)

CONTROL_SCHEMA_VERSION = 2
CONTROL_POLL_SECONDS = 0.25
CONTROL_MAX_TRANSITIONS = 128
CONTROL_MAX_BYTES = 256 * 1024
CONTROL_LOCK_TIMEOUT = 10.0
CONTROL_LOCK_RETRY_SECONDS = 0.05
CONTROL_LAUNCH_GRACE_SECONDS = 10.0
CONTROL_BINDING_RECHECK_SECONDS = 2.0


class ControlAction(StrEnum):
    START_GAME = "start_game"
    STOP_GAME = "stop_game"
    RESTART_GAME = "restart_game"
    START_EMULATOR = "start_emulator"
    STOP_EMULATOR = "stop_emulator"
    RESTART_EMULATOR = "restart_emulator"
    RESTART_ADB = "restart_adb"


class ControlState(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    WAITING_READY = "WAITING_READY"
    FINISHED = "FINISHED"


class ControlOutcome(StrEnum):
    PASS = "PASS"
    PRECONDITION_FAILED = "PRECONDITION_FAILED"
    CONFLICT = "CONFLICT"
    TIMEOUT = "TIMEOUT"
    CONTROL_FAILED = "CONTROL_FAILED"
    ABORTED = "ABORTED"


_SAFE_CONTROL_ID = re.compile(r"^[a-f0-9]{32}$")
_SAFE_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")
_SAFE_ADB_STATES = frozenset({"device", "offline", "unauthorized", "unknown", "unavailable"})
_ACTION_TIMEOUTS = {
    ControlAction.START_GAME: 60.0,
    ControlAction.STOP_GAME: 60.0,
    ControlAction.RESTART_GAME: 90.0,
    ControlAction.START_EMULATOR: 180.0,
    ControlAction.STOP_EMULATOR: 60.0,
    ControlAction.RESTART_EMULATOR: 240.0,
    ControlAction.RESTART_ADB: 60.0,
}


class RuntimeControlError(RuntimeError):
    """Ожидаемая безопасная ошибка control plane."""

    def __init__(self, code: str, message: str, *, outcome: ControlOutcome = ControlOutcome.CONTROL_FAILED) -> None:
        super().__init__(message)
        self.code = code
        self.outcome = outcome


@dataclass(frozen=True, slots=True)
class RuntimeSessionState:
    """Снимок marker DevSession для проверки owner с учётом процесса."""

    state: str | None
    process_alive: bool | None = None


@dataclass(frozen=True, slots=True)
class _RuntimeConfigSnapshot:
    serial: str
    package: str
    fingerprint: str


def _runtime_profile_payload(environment: DevEnvironment) -> Mapping[str, object]:
    from module.dev_runtime.task_sandbox import read_profile_payload

    try:
        payload = read_profile_payload(
            environment.profile_file,
            repository_root=environment.repository_root,
        )
    except Exception as exc:
        raise RuntimeControlError(
            "DEV_CONTROL_CONFIG_UNAVAILABLE",
            "Критическую конфигурацию development target невозможно прочитать",
            outcome=ControlOutcome.PRECONDITION_FAILED,
        ) from exc
    if not isinstance(payload, Mapping):
        raise RuntimeControlError(
            "DEV_CONTROL_CONFIG_INVALID",
            "Профиль development target имеет некорректную структуру",
            outcome=ControlOutcome.PRECONDITION_FAILED,
        )
    return payload


def _runtime_config_fingerprint_from_payload(
    payload: Mapping[str, object],
) -> str:
    alas = payload.get("Alas")
    if not isinstance(alas, Mapping):
        raise RuntimeControlError(
            "DEV_CONTROL_CONFIG_INVALID",
            "Профиль development target не содержит Alas-конфигурацию",
            outcome=ControlOutcome.PRECONDITION_FAILED,
        )
    relevant = {
        "alas": alas,
        "adb_server_port": os.environ.get("ANDROID_ADB_SERVER_PORT", "5037"),
    }
    try:
        canonical = json.dumps(
            relevant,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise RuntimeControlError(
            "DEV_CONTROL_CONFIG_INVALID",
            "Профиль development target нельзя канонизировать",
            outcome=ControlOutcome.PRECONDITION_FAILED,
        ) from exc
    return hashlib.sha256(canonical).hexdigest()


def runtime_config_fingerprint(environment: DevEnvironment) -> str:
    """Получить fingerprint критической runtime-конфигурации без раскрытия значений."""

    return _runtime_config_fingerprint_from_payload(_runtime_profile_payload(environment))


def _runtime_profile_value(payload: Mapping[str, object], path: str) -> object:
    current: object = payload
    for part in path.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _runtime_config_snapshot(environment: DevEnvironment) -> _RuntimeConfigSnapshot:
    payload = _runtime_profile_payload(environment)
    fingerprint = _runtime_config_fingerprint_from_payload(payload)
    serial = _runtime_profile_value(payload, "Alas.Emulator.Serial")
    package = _runtime_profile_value(payload, "Alas.Emulator.PackageName")
    if not isinstance(serial, str) or not serial.strip() or serial.strip().casefold() == "auto":
        raise RuntimeControlError(
            "DEV_TARGET_ENDPOINT_AMBIGUOUS",
            "ADB endpoint development target не определён однозначно",
            outcome=ControlOutcome.PRECONDITION_FAILED,
        )
    if not isinstance(package, str) or not package.strip() or package.strip().casefold() == "auto":
        raise RuntimeControlError(
            "DEV_TARGET_PACKAGE_UNCONFIGURED",
            "Пакет приложения development target не задан",
            outcome=ControlOutcome.PRECONDITION_FAILED,
        )
    from module.device.connection_attr import ConnectionAttr

    return _RuntimeConfigSnapshot(
        serial=ConnectionAttr.revise_serial(serial),
        package=package.strip(),
        fingerprint=fingerprint,
    )


def _environment_target(environment: DevEnvironment) -> DevTarget:
    try:
        return DevTargetRegistry.load_for_environment(
            environment.repository_root,
            fallback=environment.dev_target,
        )
    except DevTargetError as exc:
        raise RuntimeControlError(
            "DEV_CONTROL_TARGET_UNAVAILABLE",
            "Назначенный development target невозможно безопасно разрешить",
            outcome=ControlOutcome.PRECONDITION_FAILED,
        ) from exc


def _safe_fingerprint(value: object, field: str) -> str:
    if not isinstance(value, str) or not _SAFE_FINGERPRINT.fullmatch(value):
        raise RuntimeControlError(
            "DEV_CONTROL_STATE_CORRUPT",
            f"{field} control operation имеет недопустимый формат",
        )
    return value


def _timestamp(value: datetime | None = None) -> str:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    else:
        current = current.astimezone(UTC)
    return current.isoformat()


def _parse_timestamp(value: object, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or len(value) > 80:
        raise RuntimeControlError("DEV_CONTROL_STATE_CORRUPT", "Метка времени control operation некорректна")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RuntimeControlError("DEV_CONTROL_STATE_CORRUPT", "Метка времени control operation не является ISO-датой") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise RuntimeControlError("DEV_CONTROL_STATE_CORRUPT", "Метка времени control operation должна быть в UTC")
    return value


def _safe_control_id(value: object) -> str:
    if not isinstance(value, str) or not _SAFE_CONTROL_ID.fullmatch(value):
        raise RuntimeControlError("DEV_CONTROL_ID_INVALID", "control_id имеет недопустимый формат", outcome=ControlOutcome.PRECONDITION_FAILED)
    return value


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    """Ограниченный снимок состояния без serial, package и путей пользователя."""

    target_configured: bool
    emulator_detected: bool | None = None
    emulator_running: bool | None = None
    emulator_ready: bool | None = None
    adb_reachable: bool | None = None
    adb_state: str | None = None
    game_reachable: bool | None = None
    game_foreground: bool | None = None
    game_running: bool | None = None
    unrelated_adb_devices: bool | None = None

    def __post_init__(self) -> None:
        if self.adb_state is not None and self.adb_state not in _SAFE_ADB_STATES:
            object.__setattr__(self, "adb_state", "unknown")

    def as_dict(self) -> dict[str, object]:
        return {
            "development_target": {"configured": self.target_configured},
            "emulator": {
                "detected": self.emulator_detected,
                "running": self.emulator_running,
                "readiness": self.emulator_ready,
            },
            "adb": {
                "reachable": self.adb_reachable,
                "state": self.adb_state,
                "unrelated_devices": self.unrelated_adb_devices,
            },
            "game": {
                "reachable": self.game_reachable,
                "foreground": self.game_foreground,
                "running": self.game_running,
            },
        }


class RuntimeBackend(Protocol):
    def snapshot(self) -> RuntimeSnapshot: ...

    def start_emulator(self) -> object: ...

    def stop_emulator(self) -> object: ...

    def start_game(self) -> object: ...

    def stop_game(self) -> object: ...

    def restart_adb(self) -> object: ...


@dataclass(frozen=True, slots=True)
class DevRuntimeControlOperation:
    control_id: str
    action: ControlAction
    target_profile_name: str
    target_identity: str
    runtime_config_fingerprint: str
    state: ControlState
    outcome: ControlOutcome | None
    created_at: str
    started_at: str | None
    deadline_at: str
    finished_at: str | None
    transitions: tuple[dict[str, str], ...] = field(default_factory=tuple)
    supervisor_pid: int | None = None
    supervisor_created_at: float | None = None

    def __post_init__(self) -> None:
        _safe_control_id(self.control_id)
        try:
            target = DevTarget(self.target_profile_name)
        except (DevTargetError, TypeError, ValueError) as exc:
            raise RuntimeControlError(
                "DEV_CONTROL_STATE_CORRUPT",
                "Control operation содержит некорректный target profile",
            ) from exc
        if target_identity(target) != _safe_fingerprint(self.target_identity, "target_identity"):
            raise RuntimeControlError(
                "DEV_CONTROL_STATE_CORRUPT",
                "Control operation содержит несовпадающую target identity",
            )
        _safe_fingerprint(self.runtime_config_fingerprint, "runtime_config_fingerprint")
        if len(self.transitions) > CONTROL_MAX_TRANSITIONS:
            raise RuntimeControlError("DEV_CONTROL_STATE_CORRUPT", "Слишком много переходов control operation")
        if self.state is ControlState.FINISHED and self.outcome is None:
            raise RuntimeControlError("DEV_CONTROL_STATE_CORRUPT", "Завершённая control operation должна иметь outcome")
        if self.state is not ControlState.FINISHED and self.outcome is not None:
            raise RuntimeControlError("DEV_CONTROL_STATE_CORRUPT", "Активная control operation не должна иметь outcome")
        if self.state is not ControlState.CREATED and self.started_at is None:
            raise RuntimeControlError("DEV_CONTROL_STATE_CORRUPT", "Запущенная control operation должна иметь started_at")
        if self.state is ControlState.FINISHED and self.finished_at is None:
            raise RuntimeControlError("DEV_CONTROL_STATE_CORRUPT", "Завершённая control operation должна иметь finished_at")

    @property
    def active(self) -> bool:
        return self.state is not ControlState.FINISHED

    def as_dict(self, *, include_internal: bool = False) -> dict[str, object]:
        payload: dict[str, object] = {
            "control_id": self.control_id,
            "action": self.action.value,
            "target_identity": self.target_identity,
            "runtime_config_fingerprint": self.runtime_config_fingerprint,
            "state": self.state.value,
            "outcome": self.outcome.value if self.outcome is not None else None,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "deadline_at": self.deadline_at,
            "finished_at": self.finished_at,
            "transitions": [dict(item) for item in self.transitions],
        }
        if include_internal:
            payload["target_profile_name"] = self.target_profile_name
            payload["supervisor_pid"] = self.supervisor_pid
            payload["supervisor_created_at"] = self.supervisor_created_at
        return payload

    @classmethod
    def from_payload(cls, payload: object) -> DevRuntimeControlOperation:
        keys = {
            "schema_version",
            "control_id",
            "action",
            "target_profile_name",
            "target_identity",
            "runtime_config_fingerprint",
            "state",
            "outcome",
            "created_at",
            "started_at",
            "deadline_at",
            "finished_at",
            "transitions",
            "supervisor_pid",
            "supervisor_created_at",
        }
        if not isinstance(payload, Mapping) or set(payload) != keys or payload.get("schema_version") != CONTROL_SCHEMA_VERSION:
            raise RuntimeControlError("DEV_CONTROL_STATE_CORRUPT", "Control operation имеет неподдерживаемую структуру")
        try:
            action = ControlAction(str(payload["action"]))
            state = ControlState(str(payload["state"]))
            outcome_raw = payload["outcome"]
            outcome = None if outcome_raw is None else ControlOutcome(str(outcome_raw))
        except (KeyError, ValueError, TypeError) as exc:
            raise RuntimeControlError("DEV_CONTROL_STATE_CORRUPT", "Control operation содержит неизвестное состояние") from exc
        control_id = _safe_control_id(payload.get("control_id"))
        target_profile_name = payload.get("target_profile_name")
        if not isinstance(target_profile_name, str):
            raise RuntimeControlError(
                "DEV_CONTROL_STATE_CORRUPT",
                "Control operation не содержит target profile",
            )
        try:
            target_profile_name = DevTarget(target_profile_name).profile_name
        except (DevTargetError, TypeError, ValueError) as exc:
            raise RuntimeControlError(
                "DEV_CONTROL_STATE_CORRUPT",
                "Control operation содержит некорректный target profile",
            ) from exc
        target_identity_value = _safe_fingerprint(payload.get("target_identity"), "target_identity")
        runtime_config_fingerprint_value = _safe_fingerprint(
            payload.get("runtime_config_fingerprint"),
            "runtime_config_fingerprint",
        )
        if target_identity(DevTarget(target_profile_name)) != target_identity_value:
            raise RuntimeControlError(
                "DEV_CONTROL_STATE_CORRUPT",
                "Control operation содержит несовпадающую target identity",
            )
        created_at = _parse_timestamp(payload.get("created_at"))
        started_at = _parse_timestamp(payload.get("started_at"), allow_none=True)
        deadline_at = _parse_timestamp(payload.get("deadline_at"))
        finished_at = _parse_timestamp(payload.get("finished_at"), allow_none=True)
        transitions_raw = payload.get("transitions")
        if not isinstance(transitions_raw, list) or not transitions_raw or len(transitions_raw) > CONTROL_MAX_TRANSITIONS:
            raise RuntimeControlError("DEV_CONTROL_STATE_CORRUPT", "Переходы control operation имеют некорректный формат")
        transitions: list[dict[str, str]] = []
        previous_transition_at: datetime | None = None
        for item in transitions_raw:
            if not isinstance(item, Mapping) or set(item) != {"timestamp", "state", "code"}:
                raise RuntimeControlError("DEV_CONTROL_STATE_CORRUPT", "Переход control operation имеет некорректную структуру")
            transition_timestamp = _parse_timestamp(item.get("timestamp"))
            transition_at = datetime.fromisoformat(transition_timestamp or "")
            if previous_transition_at is not None and transition_at < previous_transition_at:
                raise RuntimeControlError("DEV_CONTROL_STATE_CORRUPT", "Переходы control operation идут не по времени")
            previous_transition_at = transition_at
            transition_state = item.get("state")
            transition_code = item.get("code")
            if transition_state not in {state.value for state in ControlState} or not isinstance(transition_code, str) or not re.fullmatch(r"[A-Z0-9_]{2,96}", transition_code):
                raise RuntimeControlError("DEV_CONTROL_STATE_CORRUPT", "Переход control operation имеет некорректное значение")
            transitions.append({"timestamp": transition_timestamp or "", "state": transition_state, "code": transition_code})
        if transitions[0]["state"] != ControlState.CREATED.value:
            raise RuntimeControlError("DEV_CONTROL_STATE_CORRUPT", "Первый переход control operation должен создать operation")
        if transitions[-1]["state"] != state.value:
            raise RuntimeControlError("DEV_CONTROL_STATE_CORRUPT", "Последний переход control operation не совпадает с состоянием")
        supervisor_pid = payload.get("supervisor_pid")
        if supervisor_pid is not None and (isinstance(supervisor_pid, bool) or not isinstance(supervisor_pid, int) or supervisor_pid <= 0):
            raise RuntimeControlError("DEV_CONTROL_STATE_CORRUPT", "PID supervisor control operation некорректен")
        supervisor_created_at = payload.get("supervisor_created_at")
        supervisor_created_at_value: float | None = None
        if supervisor_created_at is not None:
            if isinstance(supervisor_created_at, bool) or not isinstance(supervisor_created_at, (int, float)):
                raise RuntimeControlError("DEV_CONTROL_STATE_CORRUPT", "Время supervisor control operation некорректно")
            try:
                supervisor_created_at_value = float(supervisor_created_at)
            except (OverflowError, ValueError) as exc:
                raise RuntimeControlError("DEV_CONTROL_STATE_CORRUPT", "Время supervisor control operation некорректно") from exc
            if not math.isfinite(supervisor_created_at_value) or supervisor_created_at_value <= 0:
                raise RuntimeControlError("DEV_CONTROL_STATE_CORRUPT", "Время supervisor control operation некорректно")
        created_at_value = datetime.fromisoformat(created_at or "")
        deadline_at_value = datetime.fromisoformat(deadline_at or "")
        started_at_value = datetime.fromisoformat(started_at) if started_at is not None else None
        finished_at_value = datetime.fromisoformat(finished_at) if finished_at is not None else None
        if deadline_at_value < created_at_value:
            raise RuntimeControlError("DEV_CONTROL_STATE_CORRUPT", "Deadline control operation раньше её создания")
        if started_at_value is not None and started_at_value < created_at_value:
            raise RuntimeControlError("DEV_CONTROL_STATE_CORRUPT", "Запуск control operation раньше её создания")
        if finished_at_value is not None and started_at_value is not None and finished_at_value < started_at_value:
            raise RuntimeControlError("DEV_CONTROL_STATE_CORRUPT", "Завершение control operation раньше её запуска")
        return cls(
            control_id=control_id,
            action=action,
            target_profile_name=target_profile_name,
            target_identity=target_identity_value,
            runtime_config_fingerprint=runtime_config_fingerprint_value,
            state=state,
            outcome=outcome,
            created_at=created_at or "",
            started_at=started_at,
            deadline_at=deadline_at or "",
            finished_at=finished_at,
            transitions=tuple(transitions),
            supervisor_pid=supervisor_pid,
            supervisor_created_at=supervisor_created_at_value,
        )


def _operation_payload(operation: DevRuntimeControlOperation) -> dict[str, object]:
    payload = operation.as_dict(include_internal=True)
    payload["schema_version"] = CONTROL_SCHEMA_VERSION
    return payload


def control_operation_path(repository_root: Path) -> Path:
    """Вернуть единственный путь persisted control operation для репозитория."""

    return Path(repository_root) / "config" / "state" / "dev-runtime-control" / "operation.json"


def is_reparse_point(path: Path) -> bool:
    try:
        return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())
    except OSError as exc:
        raise RuntimeControlError("DEV_CONTROL_UNSAFE_PATH", "Путь control operation невозможно проверить") from exc


class ControlStore:
    """Одно repository-scoped persistent operation с bounded atomic storage."""

    def __init__(self, environment: DevEnvironment) -> None:
        self.environment = environment
        self.operation_path = control_operation_path(environment.repository_root)
        self.root = self.operation_path.parent
        self.lock_path = self.root / "operation.lock"
        self._check_paths()

    def _check_paths(self) -> None:
        root = self.environment.repository_root
        state = root / "config" / "state"
        if is_reparse_point(root / "config") or is_reparse_point(state):
            raise RuntimeControlError("DEV_CONTROL_UNSAFE_PATH", "Каталог control state не должен быть ссылкой или junction")
        if self.root.exists() and is_reparse_point(self.root):
            raise RuntimeControlError("DEV_CONTROL_UNSAFE_PATH", "Корень control state не должен быть ссылкой или junction")
        for path in (self.operation_path, self.lock_path):
            if os.path.lexists(path) and is_reparse_point(path):
                raise RuntimeControlError("DEV_CONTROL_UNSAFE_PATH", "Файл control state не должен быть ссылкой или junction")

    def _ensure_root(self) -> None:
        self._check_paths()
        self.root.mkdir(parents=True, exist_ok=True)
        self._check_paths()

    def read(self) -> DevRuntimeControlOperation | None:
        self._check_paths()
        try:
            raw = read_bounded_bytes(self.operation_path, max_bytes=CONTROL_MAX_BYTES)
        except FileNotFoundError:
            return None
        except BoundedReadTooLarge as exc:
            raise RuntimeControlError("DEV_CONTROL_STATE_TOO_LARGE", "Control state превышает безопасный размер") from exc
        except OSError as exc:
            raise RuntimeControlError("DEV_CONTROL_STATE_UNREADABLE", "Control state невозможно прочитать") from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise RuntimeControlError("DEV_CONTROL_STATE_CORRUPT", "Control state содержит некорректный JSON") from exc
        return DevRuntimeControlOperation.from_payload(payload)

    def write(self, operation: DevRuntimeControlOperation) -> DevRuntimeControlOperation:
        self._ensure_root()
        target = str(self.operation_path)
        temporary = to_tmp_file(target)
        try:
            file_write(temporary, json.dumps(_operation_payload(operation), ensure_ascii=True, sort_keys=True) + "\n")
            replace_tmp(temporary, target)
        finally:
            try:
                Path(temporary).unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        return operation

    def create(self, operation: DevRuntimeControlOperation) -> DevRuntimeControlOperation:
        with self.lock():
            current = self.read()
            if current is not None and current.active:
                raise RuntimeControlError("DEV_CONTROL_ACTIVE_CONFLICT", "Уже существует control operation", outcome=ControlOutcome.CONFLICT)
            return self.write(operation)

    def update(self, operation: DevRuntimeControlOperation) -> DevRuntimeControlOperation:
        with self.lock():
            return self.write(operation)

    @contextmanager
    def lock(self, *, create: bool = True) -> Iterator[None]:
        operation_exists = False
        if create:
            self._ensure_root()
        else:
            self._check_paths()
            operation_exists = self.operation_path.exists()
            if not self.root.exists() or (
                not self.lock_path.exists() and not operation_exists
            ):
                yield
                return
        handle = self.lock_path.open(
            "a+b" if create or operation_exists else "r+b"
        )
        try:
            if (create or operation_exists) and self.lock_path.stat().st_size == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            deadline = time.monotonic() + CONTROL_LOCK_TIMEOUT
            acquired = False
            try:
                if os.name == "nt":
                    import msvcrt

                    while True:
                        try:
                            handle.seek(0)
                            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                            acquired = True
                            break
                        except OSError:
                            if time.monotonic() >= deadline:
                                raise TimeoutError("Истекло время ожидания блокировки control state")
                            time.sleep(CONTROL_LOCK_RETRY_SECONDS)
                else:
                    import fcntl

                    while True:
                        try:
                            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                            acquired = True
                            break
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                raise TimeoutError("Истекло время ожидания блокировки control state")
                            time.sleep(CONTROL_LOCK_RETRY_SECONDS)
                yield
            finally:
                if acquired:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


class _ControlFailure(RuntimeControlError):
    pass


class ConfiguredRuntimeBackend:
    """Production backend без Device constructor в read-only статусе."""

    def __init__(self, environment: DevEnvironment) -> None:
        self.environment = environment
        self._app: object | None = None
        self._platform: object | None = None
        self._configuration_cache: tuple[str, str] | None = None
        self._runtime_config_fingerprint: str | None = None

    def _configuration(self, *, prepared: _RuntimeConfigSnapshot | None = None) -> tuple[str, str]:
        configuration = prepared or _runtime_config_snapshot(self.environment)
        if configuration.fingerprint != self._runtime_config_fingerprint:
            self._configuration_cache = None
            self._platform = None
            self._app = None
            self._runtime_config_fingerprint = configuration.fingerprint
        if self._configuration_cache is not None:
            return self._configuration_cache

        self._configuration_cache = (configuration.serial, configuration.package)
        return self._configuration_cache

    @staticmethod
    def _adb_client() -> object:
        import adbutils

        port = 5037
        raw_port = os.environ.get("ANDROID_ADB_SERVER_PORT")
        if raw_port:
            try:
                port = int(raw_port)
            except ValueError:
                raise RuntimeControlError("DEV_RUNTIME_ADB_CONFIG_INVALID", "Порт ADB имеет некорректный формат", outcome=ControlOutcome.PRECONDITION_FAILED) from None
        if not 1 <= port <= 65535:
            raise RuntimeControlError("DEV_RUNTIME_ADB_CONFIG_INVALID", "Порт ADB вне допустимого диапазона", outcome=ControlOutcome.PRECONDITION_FAILED)
        return adbutils.AdbClient("127.0.0.1", port)

    def _adb_device(self) -> tuple[object, list[object], str, str, object]:
        serial, package = self._configuration()
        client = self._adb_client()
        try:
            devices = list(client.device_list())
        except Exception as exc:
            raise RuntimeControlError("DEV_RUNTIME_ADB_UNREACHABLE", "ADB server недоступен", outcome=ControlOutcome.PRECONDITION_FAILED) from exc
        for device in devices:
            if str(getattr(device, "serial", "")) == serial:
                return device, devices, serial, package, client
        raise RuntimeControlError("DEV_RUNTIME_DEVICE_NOT_FOUND", "Назначенный development target не найден в ADB", outcome=ControlOutcome.PRECONDITION_FAILED)

    @staticmethod
    def _foreground_package(device: object) -> str | None:
        import re as _re

        output = device.shell(["dumpsys", "window", "windows"], timeout=5)
        match = _re.search(r"mCurrentFocus=Window\{.*?\s+(?P<package>[^\s/]+)/", str(output))
        if match:
            return match.group("package")
        match = _re.search(r"mFocusedApp=.*?\s(?P<package>[^\s/]+)/", str(output))
        return match.group("package") if match else None

    @staticmethod
    def _package_running(device: object, package: str) -> bool | None:
        try:
            output = device.shell(["pidof", package], timeout=5)
        except Exception:  # noqa: BLE001
            return None
        return bool(re.search(r"\b\d+\b", str(output)))

    def snapshot(self, *, prepared: _RuntimeConfigSnapshot | None = None) -> RuntimeSnapshot:
        try:
            serial, package = self._configuration(prepared=prepared)
            client = self._adb_client()
            devices = list(client.device_list())
        except RuntimeControlError:
            raise
        except Exception as exc:
            raise RuntimeControlError("DEV_RUNTIME_STATUS_UNAVAILABLE", "Снимок runtime status недоступен", outcome=ControlOutcome.PRECONDITION_FAILED) from exc
        target = next((item for item in devices if str(getattr(item, "serial", "")) == serial), None)
        if target is None:
            return RuntimeSnapshot(
                target_configured=True,
                emulator_detected=False,
                emulator_running=False,
                emulator_ready=False,
                adb_reachable=False,
                adb_state="unavailable",
                game_reachable=False,
                game_foreground=False,
                game_running=None,
                unrelated_adb_devices=bool(devices),
            )
        try:
            raw_state = str(getattr(target, "get_state", lambda: "unknown")())
        except Exception as exc:
            raise RuntimeControlError(
                "DEV_RUNTIME_ADB_STATE_UNAVAILABLE",
                "Состояние назначенного ADB устройства недоступно",
                outcome=ControlOutcome.PRECONDITION_FAILED,
            ) from exc
        if raw_state not in _SAFE_ADB_STATES:
            raw_state = "unknown"
        adb_ready = raw_state == "device"
        foreground: bool | None = None
        game_running: bool | None = None
        if adb_ready:
            try:
                foreground = self._foreground_package(target) == package
                game_running = True if foreground else self._package_running(target, package)
            except Exception:  # noqa: BLE001
                foreground = None
                game_running = None
        return RuntimeSnapshot(
            target_configured=True,
            emulator_detected=True,
            emulator_running=(
                True if raw_state in {"device", "offline", "unauthorized"} else None
            ),
            emulator_ready=adb_ready,
            adb_reachable=adb_ready,
            adb_state=raw_state if raw_state in _SAFE_ADB_STATES else "unknown",
            game_reachable=adb_ready,
            game_foreground=foreground,
            game_running=game_running,
            unrelated_adb_devices=any(str(getattr(item, "serial", "")) != serial for item in devices),
        )

    def _platform_for_mutation(self) -> object:
        self._configuration()
        if self._platform is None:
            from module.config.config import AzurLaneConfig
            from module.device.platform import Platform

            config = AzurLaneConfig(self.environment.profile_name, task=None)
            self._platform = Platform(config, connect=False)
        return self._platform

    def _app_controller(self) -> object:
        self._configuration()
        if self._app is None:
            device, _devices, serial, package, client = self._adb_device()
            from module.device.app_control import AppControl

            class RuntimeAppControl(AppControl):
                def __init__(self, config: object, adb_device: object, adb_serial: str, app_package: str, adb_client: object) -> None:
                    self.config = config
                    self.adb = adb_device
                    self.adb_client = adb_client
                    self.serial = adb_serial
                    self.package = app_package
                    self.is_wsa = False
                    self.is_local_network_device = False
                    self.is_waydroid = False

                def adb_shell(self, cmd: object, **kwargs: object) -> str:
                    return str(self.adb.shell(cmd, timeout=kwargs.get("timeout", 10)))

            config = getattr(self._platform_for_mutation(), "config", None)
            self._app = RuntimeAppControl(config, device, serial, package, client)
        return self._app

    def start_emulator(self) -> object:
        result = self._platform_for_mutation().emulator_start()
        if result is False:
            raise RuntimeControlError("DEV_CONTROL_EMULATOR_START_FAILED", "Platform не подтвердила запуск эмулятора")
        return result

    def stop_emulator(self) -> object:
        result = self._platform_for_mutation().emulator_stop()
        if result is False:
            raise RuntimeControlError("DEV_CONTROL_EMULATOR_STOP_FAILED", "Platform не подтвердила остановку эмулятора")
        return result

    def start_game(self) -> object:
        result = self._app_controller().app_start_adb()
        if result is False:
            raise RuntimeControlError("DEV_CONTROL_GAME_START_FAILED", "AppControl не подтвердила запуск приложения")
        return result

    def stop_game(self) -> object:
        return self._app_controller().app_stop_adb()

    def restart_adb(self) -> object:
        self._configuration()
        client = self._adb_client()
        try:
            result = client.server_kill()
            # Первый новый запрос устройств возвращает клиент adbutils
            # и даёт циклу управления реальный сигнал готовности.
            self._adb_client().device_list()
            self._app = None
        except Exception as exc:
            raise RuntimeControlError(
                "DEV_CONTROL_ADB_RESTART_FAILED",
                "ADB не перезапустился через штатный клиент",
            ) from exc
        return result


def _process_created_at(pid: int) -> float | None:
    try:
        import psutil

        return float(psutil.Process(pid).create_time())
    except Exception:  # noqa: BLE001
        return None


def _process_matches(pid: int | None, created_at: float | None) -> bool:
    if pid is None or created_at is None:
        return False
    try:
        import psutil

        process = psutil.Process(pid)
        return process.is_running() and abs(float(process.create_time()) - created_at) < 0.01
    except Exception:  # noqa: BLE001
        return False


class RuntimeControlManager:
    """Фасад status/operation API и bounded executor для control supervisor."""

    def __init__(
        self,
        environment: DevEnvironment,
        *,
        backend_factory: Callable[[DevEnvironment], RuntimeBackend] | None = None,
        session_state_provider: Callable[[], RuntimeSessionState | str | None] | None = None,
        smoke_active_provider: Callable[[], bool] | None = None,
        supervisor_launcher: Callable[[DevEnvironment, str], object] | None = None,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
        monotonic: Callable[[], float] | None = None,
        poll_seconds: float = CONTROL_POLL_SECONDS,
        action_timeouts: Mapping[ControlAction, float] | None = None,
    ) -> None:
        self.environment = environment
        self.store = ControlStore(environment)
        self.backend_factory = backend_factory or (lambda env: ConfiguredRuntimeBackend(env))
        self.session_state_provider = session_state_provider or self._default_session_state
        self.smoke_active_provider = smoke_active_provider or self._default_smoke_active
        self.supervisor_launcher = supervisor_launcher or self._launch_supervisor
        self.now = now or (lambda: datetime.now(UTC))
        self.sleep = sleep or time.sleep
        self.monotonic = monotonic or time.monotonic
        self.poll_seconds = min(max(float(poll_seconds), 0.01), 2.0)
        self.action_timeouts = dict(_ACTION_TIMEOUTS)
        if action_timeouts:
            self.action_timeouts.update({ControlAction(key): min(max(float(value), 0.01), 600.0) for key, value in action_timeouts.items()})
        self._backend: RuntimeBackend | None = None
        self._backend_environment: DevEnvironment | None = None
        self._execution_deadline: float | None = None

    def _backend_instance(self, environment: DevEnvironment | None = None) -> RuntimeBackend:
        selected_environment = environment or self.environment
        if self._backend is None or self._backend_environment != selected_environment:
            self._backend = self.backend_factory(selected_environment)
            self._backend_environment = selected_environment
        return self._backend

    def _refresh_environment(self) -> DevEnvironment:
        """Перепривязать новые операции к актуальному registry target."""

        current_target = _environment_target(self.environment)
        if current_target != self.environment.dev_target:
            self.environment = replace(self.environment, dev_target=current_target)
            self.store = ControlStore(self.environment)
            self._backend = None
            self._backend_environment = None
        return self.environment

    def _operation_environment(self, operation: DevRuntimeControlOperation) -> DevEnvironment:
        try:
            target = DevTarget(operation.target_profile_name)
        except (DevTargetError, TypeError, ValueError) as exc:
            raise RuntimeControlError(
                "DEV_CONTROL_STATE_CORRUPT",
                "Control operation содержит некорректный target profile",
            ) from exc
        if target_identity(target) != operation.target_identity:
            raise RuntimeControlError(
                "DEV_CONTROL_STATE_CORRUPT",
                "Control operation содержит несовпадающую target identity",
            )
        return replace(self.environment, dev_target=target)

    def _assert_operation_binding_details(
        self,
        operation: DevRuntimeControlOperation,
        *,
        include_configuration: bool = False,
    ) -> tuple[DevEnvironment, _RuntimeConfigSnapshot | None]:
        """Проверить binding и, при необходимости, вернуть уже прочитанную config."""

        operation_environment = self._operation_environment(operation)
        current_target = _environment_target(self.environment)
        current_identity = target_identity(current_target)
        if (
            current_target.profile_name != operation.target_profile_name
            or current_identity != operation.target_identity
        ):
            raise RuntimeControlError(
                "DEV_CONTROL_TARGET_CHANGED",
                "Development target изменился после принятия control operation",
                outcome=ControlOutcome.PRECONDITION_FAILED,
            )
        current_environment = replace(self.environment, dev_target=current_target)
        configuration = (
            _runtime_config_snapshot(current_environment)
            if include_configuration
            else None
        )
        current_config_fingerprint = (
            configuration.fingerprint
            if configuration is not None
            else runtime_config_fingerprint(current_environment)
        )
        if current_config_fingerprint != operation.runtime_config_fingerprint:
            raise RuntimeControlError(
                "DEV_CONTROL_CONFIG_CHANGED",
                "Критическая конфигурация development target изменилась после принятия control operation",
                outcome=ControlOutcome.PRECONDITION_FAILED,
            )
        return operation_environment, configuration

    def _assert_operation_binding(self, operation: DevRuntimeControlOperation) -> DevEnvironment:
        """Проверить target и config binding перед чтением или мутацией runtime."""

        environment, _configuration = self._assert_operation_binding_details(operation)
        return environment

    def _default_session_state(self) -> RuntimeSessionState | None:
        try:
            raw = read_bounded_bytes(self.environment.state_file, max_bytes=64 * 1024)
        except FileNotFoundError:
            return None
        except (BoundedReadTooLarge, OSError) as exc:
            raise RuntimeControlError("DEV_CONTROL_SESSION_STATE_UNAVAILABLE", "Состояние DevSession невозможно прочитать", outcome=ControlOutcome.PRECONDITION_FAILED) from exc
        from module.dev_runtime.contracts import DevSession

        try:
            session = DevSession.from_dict(json.loads(raw.decode("utf-8")))
        except Exception as exc:
            raise RuntimeControlError("DEV_CONTROL_SESSION_STATE_CORRUPT", "Состояние DevSession повреждено", outcome=ControlOutcome.PRECONDITION_FAILED) from exc
        process_alive = None
        if session.process is not None:
            process_alive = _process_matches(session.process.pid, session.process.created_at)
        return RuntimeSessionState(session.state.value, process_alive)

    def _default_smoke_active(self) -> bool:
        try:
            from module.dev_runtime.smoke import SmokeRunManager

            return SmokeRunManager(environment=self.environment).has_active_run()
        except RuntimeControlError:
            raise
        except Exception as exc:
            raise RuntimeControlError("DEV_CONTROL_SMOKE_STATE_UNAVAILABLE", "Состояние SmokeRun невозможно проверить", outcome=ControlOutcome.PRECONDITION_FAILED) from exc

    @staticmethod
    def _active_session(state: RuntimeSessionState | str | None) -> bool:
        if isinstance(state, RuntimeSessionState):
            lifecycle = state.state
            process_alive = state.process_alive
        else:
            lifecycle = state
            process_alive = None
        if lifecycle in {"created", "starting", "running", "stopping", "stale"}:
            return True
        return lifecycle in {"failed", "stopped"} and process_alive is True

    @staticmethod
    def _requires_idle_session(action: ControlAction) -> bool:
        return action in {
            ControlAction.START_GAME,
            ControlAction.STOP_GAME,
            ControlAction.RESTART_GAME,
            ControlAction.START_EMULATOR,
            ControlAction.STOP_EMULATOR,
            ControlAction.RESTART_EMULATOR,
            ControlAction.RESTART_ADB,
        }

    def _conflict(self, action: ControlAction) -> RuntimeControlError | None:
        try:
            if self.smoke_active_provider():
                return RuntimeControlError("DEV_CONTROL_CONFLICT_SMOKE_ACTIVE", "Runtime control запрещён при активном SmokeRun", outcome=ControlOutcome.CONFLICT)
            session_state = self.session_state_provider()
        except RuntimeControlError:
            raise
        except Exception as exc:
            raise RuntimeControlError("DEV_CONTROL_PRECONDITION_UNKNOWN", "Нельзя подтвердить отсутствие активного runtime владельца", outcome=ControlOutcome.PRECONDITION_FAILED) from exc
        if self._requires_idle_session(action) and self._active_session(session_state):
            return RuntimeControlError("DEV_CONTROL_CONFLICT_DEV_SESSION", "Runtime control запрещён при активной DevSession", outcome=ControlOutcome.CONFLICT)
        return None

    def _result(self, *, ok: bool, code: str, message: str, state: str, details: Mapping[str, object] | None = None) -> DevResult:
        return DevResult(ok=ok, code=code, message=message, state=state, details=dict(details or {}))

    def _operation_result(self, operation: DevRuntimeControlOperation, *, code: str, message: str, ok: bool = True) -> DevResult:
        return self._result(ok=ok, code=code, message=message, state=operation.state.value, details={"control_operation": operation.as_dict()})

    def _reserve_operation(self, action: ControlAction) -> DevRuntimeControlOperation:
        """Атомарно проверить владельцев и создать persistent control reservation."""

        with runtime_coordination_lock(self.environment):
            environment = self._refresh_environment()
            conflict = self._conflict(action)
            if conflict is not None:
                raise conflict
            # После обрыва питания persisted operation может остаться активной
            # без supervisor. Сначала закрываем её fail-closed, затем разрешаем
            # следующий запрос через обычный single-operation guard.
            self._reconcile()
            now = self.now()
            created_at = _timestamp(now)
            deadline_at = _timestamp(now + timedelta(seconds=self.action_timeouts[action]))
            target = environment.dev_target
            if target is None:  # pragma: no cover - защищено DevEnvironment
                raise RuntimeControlError(
                    "DEV_CONTROL_TARGET_UNAVAILABLE",
                    "Development target не назначен",
                    outcome=ControlOutcome.PRECONDITION_FAILED,
                )
            operation = DevRuntimeControlOperation(
                control_id=uuid.uuid4().hex,
                action=action,
                target_profile_name=target.profile_name,
                target_identity=target_identity(target),
                runtime_config_fingerprint=runtime_config_fingerprint(environment),
                state=ControlState.CREATED,
                outcome=None,
                created_at=created_at,
                started_at=None,
                deadline_at=deadline_at,
                finished_at=None,
                transitions=({"timestamp": created_at, "state": ControlState.CREATED.value, "code": "DEV_CONTROL_CREATED"},),
            )
            self.store.create(operation)
            return operation

    def status(self) -> DevResult:
        try:
            environment = self._refresh_environment()
            snapshot = self._backend_instance(environment).snapshot()
            status_ok = True
            status_code = "DEV_RUNTIME_STATUS_READY"
            message = "Текущий runtime status development target прочитан"
        except RuntimeControlError as exc:
            snapshot = RuntimeSnapshot(target_configured=True)
            status_ok = False
            status_code = exc.code
            message = str(exc)
        try:
            operation = self._reconcile(read_only=True)
            control_details: dict[str, object] = {"active": operation is not None and operation.active}
            if operation is not None:
                control_details["operation"] = operation.as_dict()
        except RuntimeControlError as exc:
            operation = None
            control_details = {"active": None, "code": exc.code}
            status_ok = False
            status_code = exc.code
            message = str(exc)
        try:
            session_state = self.session_state_provider()
            smoke_active = self.smoke_active_provider()
        except (RuntimeControlError, ValueError, OSError, TimeoutError) as exc:
            session_state = None
            smoke_active = None
            status_ok = False
            status_code = (
                exc.code
                if isinstance(exc, RuntimeControlError)
                else "DEV_CONTROL_PRECONDITION_UNKNOWN"
            )
            message = str(exc)
        details = snapshot.as_dict()
        details["dev_session"] = {
            "state": session_state.state if isinstance(session_state, RuntimeSessionState) else session_state
        }
        details["smoke"] = {"active": smoke_active}
        details["control_operation"] = control_details
        return self._result(ok=status_ok, code=status_code, message=message, state="ready" if status_ok else "failed", details=details)

    def start(self, action: ControlAction | str) -> DevResult:
        try:
            action = ControlAction(action)
        except (ValueError, TypeError):
            return self._result(ok=False, code="DEV_CONTROL_ACTION_INVALID", message="Неизвестное действие runtime control", state="failed", details={"outcome": ControlOutcome.PRECONDITION_FAILED.value})
        try:
            operation = self._reserve_operation(action)
            try:
                self.supervisor_launcher(self.environment, operation.control_id)
                # Не записываем PID, возвращённый launcher-ом. На Windows
                # .venv\Scripts\python.exe является redirector и возвращает
                # PID родительского процесса, тогда как control supervisor
                # должен claim-ить собственный PID. Иначе родительская запись
                # блокирует claim дочернего процесса и после выхода redirector-а
                # operation ошибочно закрывается как аварийная.
                with self.store.lock():
                    current = self.store.read()
                    if current is not None and current.control_id == operation.control_id:
                        operation = current
            except Exception:  # noqa: BLE001
                with self.store.lock():
                    current = self.store.read()
                    if current is not None and current.control_id == operation.control_id:
                        if current.state is ControlState.FINISHED or current.supervisor_pid is not None:
                            operation = current
                        else:
                            operation = self._finish(
                                current,
                                outcome=ControlOutcome.ABORTED,
                                code="DEV_CONTROL_SUPERVISOR_START_FAILED",
                            )
                            self.store.write(operation)
                return self._operation_result(operation, ok=False, code="DEV_CONTROL_SUPERVISOR_START_FAILED", message="Supervisor control operation не запустился")
            if operation.state is ControlState.FINISHED:
                return self._operation_result(
                    operation,
                    ok=operation.outcome is ControlOutcome.PASS,
                    code=(
                        "DEV_CONTROL_FINISHED"
                        if operation.outcome is ControlOutcome.PASS
                        else "DEV_CONTROL_SUPERVISOR_IDENTITY_UNAVAILABLE"
                    ),
                    message=(
                        "Control operation завершена supervisor-процессом"
                        if operation.outcome is ControlOutcome.PASS
                        else "Supervisor control operation не имеет проверяемой личности"
                    ),
                )
            return self._operation_result(operation, code="DEV_CONTROL_ACCEPTED", message="Control operation принята supervisor-процессом")
        except RuntimeCoordinationError as exc:
            return self._result(
                ok=False,
                code=exc.code,
                message=str(exc),
                state="failed",
                details={"outcome": ControlOutcome.CONTROL_FAILED.value},
            )
        except RuntimeControlError as exc:
            return self._result(ok=False, code=exc.code, message=str(exc), state="conflict" if exc.outcome is ControlOutcome.CONFLICT else "failed", details={"outcome": exc.outcome.value})
        except (OSError, TimeoutError):
            return self._result(ok=False, code="DEV_CONTROL_STATE_UNAVAILABLE", message="Control operation невозможно сохранить", state="failed", details={"outcome": ControlOutcome.CONTROL_FAILED.value})

    def get_operation(self, control_id: str) -> DevResult:
        try:
            _safe_control_id(control_id)
            operation = self._reconcile(control_id=control_id)
            if operation is None:
                return self._result(ok=False, code="DEV_CONTROL_NOT_FOUND", message="Control operation не найдена", state=ControlState.FINISHED.value)
            return self._operation_result(
                operation,
                code="DEV_CONTROL_OPERATION_READY",
                message="Состояние control operation прочитано",
                ok=operation.outcome in {None, ControlOutcome.PASS},
            )
        except RuntimeControlError as exc:
            return self._result(ok=False, code=exc.code, message=str(exc), state="failed", details={"outcome": exc.outcome.value})

    def _within_launch_grace(self, operation: DevRuntimeControlOperation) -> bool:
        if operation.state is not ControlState.CREATED or operation.supervisor_pid is not None:
            return False
        try:
            created_at = datetime.fromisoformat(operation.created_at)
            current = self.now()
            if current.tzinfo is None:
                current = current.replace(tzinfo=UTC)
            else:
                current = current.astimezone(UTC)
            age = (current - created_at).total_seconds()
            return 0 <= age < CONTROL_LAUNCH_GRACE_SECONDS
        except (OverflowError, TypeError, ValueError):
            return False

    def _latest_persisted_operation(
        self,
        fallback: DevRuntimeControlOperation,
    ) -> DevRuntimeControlOperation:
        try:
            with self.store.lock():
                current = self.store.read()
        except (OSError, RuntimeControlError, TimeoutError):
            return fallback
        if current is None or current.control_id != fallback.control_id:
            return fallback
        return current

    def _finish_from_latest(
        self,
        operation: DevRuntimeControlOperation,
        *,
        outcome: ControlOutcome,
        code: str,
    ) -> DevRuntimeControlOperation:
        latest = self._latest_persisted_operation(operation)
        if latest.state is ControlState.FINISHED:
            return latest
        return self._finish(latest, outcome=outcome, code=code)

    def _invalidate_operation(
        self,
        operation: DevRuntimeControlOperation,
        error: RuntimeControlError,
    ) -> DevRuntimeControlOperation:
        """Зафиксировать fail-closed binding mismatch без вызова backend."""

        with self.store.lock():
            current = self.store.read()
            if current is None or current.control_id != operation.control_id:
                return operation
            if current.state is ControlState.FINISHED:
                return current
            finished = self._finish(
                current,
                outcome=error.outcome,
                code=error.code,
            )
            self.store.write(finished)
            return finished

    def _reconcile(self, *, control_id: str | None = None, read_only: bool = False) -> DevRuntimeControlOperation | None:
        with self.store.lock(create=not read_only):
            operation = self.store.read()
            if operation is None or (control_id is not None and operation.control_id != control_id):
                return None
            if operation.active and not self._within_launch_grace(operation) and (
                operation.supervisor_pid is None
                or not _process_matches(operation.supervisor_pid, operation.supervisor_created_at)
            ):
                code = (
                    "DEV_CONTROL_SUPERVISOR_IDENTITY_UNAVAILABLE"
                    if operation.supervisor_pid is None
                    else "DEV_CONTROL_SUPERVISOR_CRASHED"
                )
                operation = self._finish(operation, outcome=ControlOutcome.ABORTED, code=code)
                if not read_only:
                    self.store.write(operation)
            return operation

    def _launch_supervisor(self, environment: DevEnvironment, control_id: str) -> subprocess.Popen[bytes]:
        command = [
            str(environment.python_executable),
            "-m",
            "module.dev_runtime.control_supervisor",
            "--operation-id",
            _safe_control_id(control_id),
        ]
        creationflags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        kwargs: dict[str, object] = {
            "cwd": str(environment.repository_root),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
        }
        if os.name == "nt":
            kwargs["creationflags"] = creationflags
        else:
            kwargs["start_new_session"] = True
        return subprocess.Popen(
            command,
            **kwargs,
        )

    def _finish(self, operation: DevRuntimeControlOperation, *, outcome: ControlOutcome, code: str) -> DevRuntimeControlOperation:
        finished_at = _timestamp(self.now())
        transition = {"timestamp": finished_at, "state": ControlState.FINISHED.value, "code": code}
        return replace(
            operation,
            state=ControlState.FINISHED,
            outcome=outcome,
            started_at=operation.started_at or finished_at,
            finished_at=finished_at,
            transitions=(*operation.transitions[-(CONTROL_MAX_TRANSITIONS - 1):], transition),
        )

    def _transition(self, operation: DevRuntimeControlOperation, state: ControlState, code: str) -> DevRuntimeControlOperation:
        timestamp = _timestamp(self.now())
        transition = {"timestamp": timestamp, "state": state.value, "code": code}
        updated = replace(operation, state=state, started_at=operation.started_at or timestamp, transitions=(*operation.transitions[-(CONTROL_MAX_TRANSITIONS - 1):], transition))
        with self.store.lock():
            self.store.write(updated)
        return updated

    def _claim_supervisor(self, operation: DevRuntimeControlOperation) -> DevRuntimeControlOperation | DevResult:
        """Атомарно закрепить единственный supervisor за operation."""

        pid = os.getpid()
        created_at = _process_created_at(pid)
        if created_at is None:
            finished = self._finish(
                operation,
                outcome=ControlOutcome.ABORTED,
                code="DEV_CONTROL_SUPERVISOR_IDENTITY_UNAVAILABLE",
            )
            self.store.update(finished)
            return self._operation_result(
                finished,
                ok=False,
                code="DEV_CONTROL_SUPERVISOR_IDENTITY_UNAVAILABLE",
                message="Supervisor control operation не имеет проверяемой личности",
            )
        with self.store.lock():
            current = self.store.read()
            if current is None or current.control_id != operation.control_id:
                return self._result(
                    ok=False,
                    code="DEV_CONTROL_NOT_FOUND",
                    message="Control operation не найдена",
                    state=ControlState.FINISHED.value,
                )
            if current.state is ControlState.FINISHED:
                return self._operation_result(
                    current,
                    code="DEV_CONTROL_OPERATION_READY",
                    message="Control operation уже завершена",
                    ok=current.outcome is ControlOutcome.PASS,
                )
            if (
                current.supervisor_pid is not None
                and (current.supervisor_pid != pid or current.supervisor_created_at != created_at)
            ):
                if _process_matches(current.supervisor_pid, current.supervisor_created_at):
                    return self._operation_result(
                        current,
                        code="DEV_CONTROL_IN_PROGRESS",
                        message="Control operation уже выполняется другим supervisor",
                    )
                current = self._finish(
                    current,
                    outcome=ControlOutcome.ABORTED,
                    code="DEV_CONTROL_SUPERVISOR_CRASHED",
                )
                self.store.write(current)
                return self._operation_result(
                    current,
                    ok=False,
                    code="DEV_CONTROL_SUPERVISOR_CRASHED",
                    message="Предыдущий supervisor control operation завершился аварийно",
                )
            claimed = replace(current, supervisor_pid=pid, supervisor_created_at=created_at)
            self.store.write(claimed)
            return claimed

    def _before_deadline(self, operation: DevRuntimeControlOperation) -> bool:
        if self._execution_deadline is not None:
            return self.monotonic() < self._execution_deadline
        try:
            current = self.now()
            if current.tzinfo is None:
                current = current.replace(tzinfo=UTC)
            return current.astimezone(UTC) < datetime.fromisoformat(operation.deadline_at)
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _backend_snapshot(
        backend: RuntimeBackend,
        prepared: _RuntimeConfigSnapshot | None,
    ) -> RuntimeSnapshot:
        if prepared is not None and isinstance(backend, ConfiguredRuntimeBackend):
            return backend.snapshot(prepared=prepared)
        return backend.snapshot()

    def _wait_for(self, operation: DevRuntimeControlOperation, backend: RuntimeBackend, predicate: Callable[[RuntimeSnapshot], bool]) -> tuple[DevRuntimeControlOperation, RuntimeSnapshot]:
        operation = self._transition(operation, ControlState.WAITING_READY, "DEV_CONTROL_WAITING_READY")
        prepared: _RuntimeConfigSnapshot | None = None
        binding_checked = False
        next_binding_check = float("-inf")
        while self._before_deadline(operation):
            now = self.monotonic()
            if not binding_checked or now >= next_binding_check:
                _environment, prepared = self._assert_operation_binding_details(
                    operation,
                    include_configuration=isinstance(backend, ConfiguredRuntimeBackend),
                )
                binding_checked = True
                next_binding_check = now + CONTROL_BINDING_RECHECK_SECONDS
            snapshot = self._backend_snapshot(backend, prepared)
            if predicate(snapshot):
                return operation, snapshot
            self.sleep(min(self.poll_seconds, 0.5))
        raise _ControlFailure("DEV_CONTROL_TIMEOUT", "Control operation не дождалась подтверждённого состояния", outcome=ControlOutcome.TIMEOUT)

    @staticmethod
    def _require(value: bool | None, code: str, message: str) -> None:
        if value is not True:
            raise _ControlFailure(code, message, outcome=ControlOutcome.PRECONDITION_FAILED)

    @staticmethod
    def _call(result: object, code: str, message: str) -> None:
        if result is False:
            raise _ControlFailure(code, message)

    def _execute_action(self, operation: DevRuntimeControlOperation, backend: RuntimeBackend) -> DevRuntimeControlOperation:
        if not self._before_deadline(operation):
            raise _ControlFailure("DEV_CONTROL_TIMEOUT", "Control operation истекла до запуска", outcome=ControlOutcome.TIMEOUT)
        prepared: _RuntimeConfigSnapshot | None = None
        if isinstance(backend, ConfiguredRuntimeBackend):
            _environment, prepared = self._assert_operation_binding_details(
                operation,
                include_configuration=True,
            )
        else:
            self._assert_operation_binding(operation)
        if operation.state is ControlState.CREATED:
            operation = self._transition(operation, ControlState.RUNNING, "DEV_CONTROL_STARTED")
        action = operation.action
        snapshot = self._backend_snapshot(backend, prepared)
        if action is ControlAction.START_EMULATOR:
            if snapshot.emulator_running is True and snapshot.emulator_ready is True:
                return self._finish(operation, outcome=ControlOutcome.PASS, code="DEV_CONTROL_ALREADY_READY")
            if snapshot.emulator_running is True:
                operation, _ = self._wait_for(operation, backend, lambda item: item.emulator_running is True and item.emulator_ready is True)
                return self._finish(operation, outcome=ControlOutcome.PASS, code="DEV_CONTROL_READY")
            if snapshot.emulator_running is None:
                raise _ControlFailure("DEV_CONTROL_EMULATOR_STATE_UNKNOWN", "Нельзя безопасно подтвердить, что эмулятор остановлен", outcome=ControlOutcome.PRECONDITION_FAILED)
            self._assert_operation_binding(operation)
            self._call(backend.start_emulator(), "DEV_CONTROL_EMULATOR_START_FAILED", "Platform не запустила эмулятор")
            operation, _ = self._wait_for(operation, backend, lambda item: item.emulator_running is True and item.emulator_ready is True)
            return self._finish(operation, outcome=ControlOutcome.PASS, code="DEV_CONTROL_READY")
        if action is ControlAction.STOP_EMULATOR:
            if snapshot.emulator_running is False and snapshot.emulator_detected is False:
                return self._finish(operation, outcome=ControlOutcome.PASS, code="DEV_CONTROL_ALREADY_STOPPED")
            self._require(snapshot.emulator_running is True or snapshot.emulator_detected is True, "DEV_CONTROL_EMULATOR_STATE_UNKNOWN", "Нельзя безопасно подтвердить состояние эмулятора")
            self._assert_operation_binding(operation)
            self._call(backend.stop_emulator(), "DEV_CONTROL_EMULATOR_STOP_FAILED", "Platform не остановила эмулятор")
            operation, _ = self._wait_for(operation, backend, lambda item: item.emulator_running is False and item.emulator_detected is False)
            return self._finish(operation, outcome=ControlOutcome.PASS, code="DEV_CONTROL_STOPPED")
        if action is ControlAction.RESTART_EMULATOR:
            self._require(snapshot.emulator_running, "DEV_CONTROL_EMULATOR_NOT_RUNNING", "Перезапуск требует подтверждённого работающего эмулятора")
            self._assert_operation_binding(operation)
            self._call(backend.stop_emulator(), "DEV_CONTROL_EMULATOR_STOP_FAILED", "Platform не остановила эмулятор")
            operation, _ = self._wait_for(operation, backend, lambda item: item.emulator_running is False and item.emulator_detected is False)
            self._assert_operation_binding(operation)
            self._call(backend.start_emulator(), "DEV_CONTROL_EMULATOR_START_FAILED", "Platform не запустила эмулятор")
            operation, _ = self._wait_for(operation, backend, lambda item: item.emulator_running is True and item.emulator_ready is True)
            return self._finish(operation, outcome=ControlOutcome.PASS, code="DEV_CONTROL_RESTARTED")
        if action in {
            ControlAction.START_GAME,
            ControlAction.STOP_GAME,
            ControlAction.RESTART_GAME,
        }:
            self._require(snapshot.emulator_ready, "DEV_CONTROL_EMULATOR_NOT_READY", "Эмулятор не готов для управления приложением")
            self._require(snapshot.adb_reachable, "DEV_CONTROL_ADB_UNREACHABLE", "ADB недоступен для управления приложением")
        if action is ControlAction.START_GAME:
            if snapshot.game_foreground is True:
                return self._finish(operation, outcome=ControlOutcome.PASS, code="DEV_CONTROL_GAME_ALREADY_FOREGROUND")
            self._assert_operation_binding(operation)
            self._call(backend.start_game(), "DEV_CONTROL_GAME_START_FAILED", "AppControl не запустила приложение")
            operation, _ = self._wait_for(operation, backend, lambda item: item.game_foreground is True)
            return self._finish(operation, outcome=ControlOutcome.PASS, code="DEV_CONTROL_GAME_READY")
        if action is ControlAction.STOP_GAME:
            if snapshot.game_running is False:
                return self._finish(operation, outcome=ControlOutcome.PASS, code="DEV_CONTROL_GAME_ALREADY_STOPPED")
            self._require(snapshot.game_running is True, "DEV_CONTROL_GAME_STATE_UNKNOWN", "Нельзя безопасно подтвердить состояние приложения")
            self._assert_operation_binding(operation)
            self._call(backend.stop_game(), "DEV_CONTROL_GAME_STOP_FAILED", "AppControl не остановила приложение")
            operation, _ = self._wait_for(operation, backend, lambda item: item.game_running is False)
            return self._finish(operation, outcome=ControlOutcome.PASS, code="DEV_CONTROL_GAME_STOPPED")
        if action is ControlAction.RESTART_GAME:
            self._require(snapshot.game_running is True, "DEV_CONTROL_GAME_STATE_UNKNOWN", "Нельзя безопасно подтвердить состояние приложения")
            self._assert_operation_binding(operation)
            self._call(backend.stop_game(), "DEV_CONTROL_GAME_STOP_FAILED", "AppControl не остановила приложение")
            operation, _ = self._wait_for(operation, backend, lambda item: item.game_running is False)
            self._assert_operation_binding(operation)
            self._call(backend.start_game(), "DEV_CONTROL_GAME_START_FAILED", "AppControl не запустила приложение")
            operation, _ = self._wait_for(operation, backend, lambda item: item.game_foreground is True)
            return self._finish(operation, outcome=ControlOutcome.PASS, code="DEV_CONTROL_GAME_RESTARTED")
        self._require(snapshot.unrelated_adb_devices is False, "DEV_CONTROL_ADB_UNRELATED_DEVICES", "Перезапуск ADB затронет неизвестные устройства")
        self._assert_operation_binding(operation)
        self._call(backend.restart_adb(), "DEV_CONTROL_ADB_RESTART_FAILED", "ADB не перезапустился")
        operation, _ = self._wait_for(operation, backend, lambda item: item.adb_reachable is True)
        return self._finish(operation, outcome=ControlOutcome.PASS, code="DEV_CONTROL_ADB_READY")

    def execute(self, control_id: str) -> DevResult:
        try:
            _safe_control_id(control_id)
            with self.store.lock():
                operation = self.store.read()
            if operation is None or operation.control_id != control_id:
                return self._result(ok=False, code="DEV_CONTROL_NOT_FOUND", message="Control operation не найдена", state=ControlState.FINISHED.value)
            if operation.state is ControlState.FINISHED:
                return self._operation_result(
                    operation,
                    code="DEV_CONTROL_OPERATION_READY",
                    message="Control operation уже завершена",
                    ok=operation.outcome is ControlOutcome.PASS,
                )
            try:
                self._assert_operation_binding(operation)
            except RuntimeControlError as exc:
                finished = self._invalidate_operation(operation, exc)
                return self._operation_result(
                    finished,
                    ok=False,
                    code=exc.code,
                    message=str(exc),
                )
            claimed = self._claim_supervisor(operation)
            if isinstance(claimed, DevResult):
                return claimed
            operation = claimed
            with self.store.lock():
                current = self.store.read()
                if current is None or current.control_id != control_id:
                    return self._result(ok=False, code="DEV_CONTROL_NOT_FOUND", message="Control operation не найдена", state=ControlState.FINISHED.value)
                operation = current
                if operation.state is ControlState.FINISHED:
                    return self._operation_result(operation, code="DEV_CONTROL_OPERATION_READY", message="Control operation уже завершена", ok=operation.outcome is ControlOutcome.PASS)
                try:
                    self._assert_operation_binding(operation)
                except RuntimeControlError as exc:
                    finished = self._finish(operation, outcome=exc.outcome, code=exc.code)
                    self.store.write(finished)
                    return self._operation_result(
                        finished,
                        ok=False,
                        code=exc.code,
                        message=str(exc),
                    )
                started_at = _timestamp(self.now())
                operation = replace(
                    operation,
                    state=ControlState.RUNNING,
                    started_at=operation.started_at or started_at,
                    transitions=(*operation.transitions[-(CONTROL_MAX_TRANSITIONS - 1):], {"timestamp": started_at, "state": ControlState.RUNNING.value, "code": "DEV_CONTROL_STARTED"}),
                )
                self.store.write(operation)
                try:
                    deadline = datetime.fromisoformat(operation.deadline_at)
                    current_time = self.now()
                    if current_time.tzinfo is None:
                        current_time = current_time.replace(tzinfo=UTC)
                    remaining = (deadline - current_time.astimezone(UTC)).total_seconds()
                    self._execution_deadline = self.monotonic() + max(0.0, remaining)
                except (TypeError, ValueError):
                    self._execution_deadline = self.monotonic()
            try:
                execution_environment = self._assert_operation_binding(operation)
                finished = self._execute_action(
                    operation,
                    self._backend_instance(execution_environment),
                )
            except RuntimeControlError as exc:
                finished = self._finish_from_latest(
                    operation,
                    outcome=exc.outcome,
                    code=exc.code,
                )
            except Exception:  # noqa: BLE001
                finished = self._finish_from_latest(
                    operation,
                    outcome=ControlOutcome.CONTROL_FAILED,
                    code="DEV_CONTROL_UNEXPECTED_FAILURE",
                )
            with self.store.lock():
                current = self.store.read()
                if current is None or current.control_id != operation.control_id:
                    return self._result(
                        ok=False,
                        code="DEV_CONTROL_STATE_CHANGED",
                        message="Control operation изменилась до фиксации результата",
                        state="failed",
                    )
                if current.state is not ControlState.FINISHED:
                    self.store.write(finished)
                else:
                    finished = current
            result_code = "DEV_CONTROL_FINISHED"
            if finished.state is ControlState.FINISHED and finished.outcome is not ControlOutcome.PASS and finished.transitions:
                result_code = finished.transitions[-1]["code"]
            return self._operation_result(finished, code=result_code if finished.state is ControlState.FINISHED else "DEV_CONTROL_OPERATION_READY", message="Control operation завершена" if finished.state is ControlState.FINISHED else "Control operation выполняется", ok=finished.outcome is ControlOutcome.PASS)
        except RuntimeControlError as exc:
            return self._result(ok=False, code=exc.code, message=str(exc), state="failed", details={"outcome": exc.outcome.value})
        finally:
            self._execution_deadline = None


__all__ = [
    "CONTROL_POLL_SECONDS",
    "CONTROL_SCHEMA_VERSION",
    "ConfiguredRuntimeBackend",
    "ControlAction",
    "ControlOutcome",
    "ControlState",
    "ControlStore",
    "DevRuntimeControlOperation",
    "RuntimeBackend",
    "RuntimeControlError",
    "RuntimeControlManager",
    "RuntimeSessionState",
    "RuntimeSnapshot",
    "control_operation_path",
    "is_reparse_point",
    "runtime_config_fingerprint",
]
