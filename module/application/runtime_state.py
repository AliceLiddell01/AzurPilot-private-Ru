"""Нейтральное process-shared состояние runtime-профилей.

Модуль не знает о WebUI, MCP, Dev Runtime и persistence. Он хранит только
типизированный снимок runtime в текущей рабочей копии. Фактический владелец
процесса обязан передавать ``worker_created_at`` и подтверждать его через
собственный owner-specific adapter.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from deploy.atomic import atomic_write
from module.application.host_lock import (
    HOST_LOCK_TIMEOUT_SECONDS,
    application_host_lock,
)

_STATE_SCHEMA_VERSION = 2
_MAX_STATE_BYTES = 256 * 1024
_MAX_PROFILES = 128
_MAX_TEXT = 256
_MAX_TASK = 256
_FRESHNESS_SECONDS = 120.0
_SAFE_PROFILE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SAFE_PHASE = re.compile(r"^[a-z_]{1,64}$")


class RuntimeStateError(RuntimeError):
    """Безопасная ошибка чтения или записи process-shared runtime state."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


class RuntimePhase(StrEnum):
    STOPPED = "stopped"
    USER_PROFILE_IDLE = "user_profile_idle"
    USER_PROFILE_BUSY = "user_profile_busy"
    HANDOVER_REQUESTED = "handover_requested"
    PREEMPTION_NOTICE = "preemption_notice"
    GRACE_PERIOD = "grace_period"
    QUIESCE_REQUESTED = "quiesce_requested"
    CURRENT_TASK_DRAINING = "current_task_draining"
    CURRENT_TASK_STOPPED = "current_task_stopped"
    RETURNING_TO_MAIN = "returning_to_main"
    MAIN_CONFIRMED = "main_confirmed"
    RESOURCE_ACQUIRING = "resource_acquiring"
    RESOURCE_READY = "resource_ready"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RuntimeStateSnapshot:
    profile: str
    phase: RuntimePhase
    worker_running: bool
    busy: bool
    current_task: str | None
    operation_id: str | None
    session_id: str | None
    handover_requested: bool
    draining: bool
    stop_requested: bool
    terminal_state: str | None
    worker_pid: int | None
    worker_created_at: float | None
    updated_at: str
    freshness: str
    provenance: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": _STATE_SCHEMA_VERSION,
            "profile": self.profile,
            "phase": self.phase.value,
            "worker_running": self.worker_running,
            "busy": self.busy,
            "current_task": self.current_task,
            "operation_id": self.operation_id,
            "session_id": self.session_id,
            "handover_requested": self.handover_requested,
            "draining": self.draining,
            "stop_requested": self.stop_requested,
            "terminal_state": self.terminal_state,
            "worker_pid": self.worker_pid,
            "worker_created_at": self.worker_created_at,
            "updated_at": self.updated_at,
            "freshness": self.freshness,
            "provenance": self.provenance,
        }

    @classmethod
    def from_dict(cls, payload: object) -> RuntimeStateSnapshot:
        if not isinstance(payload, Mapping):
            raise RuntimeStateError("RUNTIME_STATE_CORRUPT", "Runtime state должен быть объектом")
        required = {
            "schema_version",
            "profile",
            "phase",
            "worker_running",
            "busy",
            "current_task",
            "operation_id",
            "session_id",
            "handover_requested",
            "draining",
            "stop_requested",
            "terminal_state",
            "worker_pid",
            "worker_created_at",
            "updated_at",
            "freshness",
            "provenance",
        }
        if set(payload) != required or payload.get("schema_version") != _STATE_SCHEMA_VERSION:
            raise RuntimeStateError("RUNTIME_STATE_CORRUPT", "Runtime state имеет неизвестные поля")

        profile = _profile(payload.get("profile"))
        try:
            phase = RuntimePhase(str(payload["phase"]))
        except (KeyError, ValueError) as exc:
            raise RuntimeStateError("RUNTIME_STATE_CORRUPT", "Runtime state содержит неизвестную phase") from exc
        booleans = (
            payload.get("worker_running"),
            payload.get("busy"),
            payload.get("handover_requested"),
            payload.get("draining"),
            payload.get("stop_requested"),
        )
        if not all(type(value) is bool for value in booleans):
            raise RuntimeStateError("RUNTIME_STATE_CORRUPT", "Runtime state содержит некорректные флаги")
        current_task = _optional_text(payload.get("current_task"), maximum=_MAX_TASK)
        operation_id = _optional_token(payload.get("operation_id"))
        session_id = _optional_token(payload.get("session_id"))
        terminal_state = _optional_text(payload.get("terminal_state"), maximum=_MAX_TEXT)
        worker_pid = payload.get("worker_pid")
        if worker_pid is not None and (
            isinstance(worker_pid, bool) or not isinstance(worker_pid, int) or worker_pid <= 0
        ):
            raise RuntimeStateError("RUNTIME_STATE_CORRUPT", "worker_pid имеет некорректный формат")
        worker_created_at = payload.get("worker_created_at")
        if worker_created_at is not None:
            if (
                isinstance(worker_created_at, bool)
                or not isinstance(worker_created_at, (int, float))
                or not math.isfinite(float(worker_created_at))
                or float(worker_created_at) <= 0
            ):
                raise RuntimeStateError(
                    "RUNTIME_STATE_CORRUPT", "worker_created_at имеет некорректный формат"
                )
            worker_created_at = float(worker_created_at)
        if payload["worker_running"] is True and (
            worker_pid is None or worker_created_at is None
        ):
            raise RuntimeStateError(
                "RUNTIME_STATE_CORRUPT",
                "Работающий worker должен иметь подтверждённую identity",
            )
        if payload["worker_running"] is False and (
            worker_pid is not None or worker_created_at is not None
        ):
            raise RuntimeStateError(
                "RUNTIME_STATE_CORRUPT",
                "Остановленный worker не должен иметь активную identity",
            )
        if payload["busy"] is True and (
            payload["worker_running"] is not True or current_task is None
        ):
            raise RuntimeStateError(
                "RUNTIME_STATE_CORRUPT",
                "Занятый worker должен иметь активную задачу и identity",
            )
        if payload["busy"] is False and current_task is not None:
            raise RuntimeStateError(
                "RUNTIME_STATE_CORRUPT",
                "Свободный worker не должен иметь текущую задачу",
            )
        updated_at = payload.get("updated_at")
        _timestamp(updated_at)
        freshness = payload.get("freshness")
        if not isinstance(freshness, str) or not _SAFE_PHASE.fullmatch(freshness):
            raise RuntimeStateError("RUNTIME_STATE_CORRUPT", "freshness имеет некорректный формат")
        provenance = payload.get("provenance")
        if not isinstance(provenance, str) or not _SAFE_PHASE.fullmatch(provenance):
            raise RuntimeStateError("RUNTIME_STATE_CORRUPT", "provenance имеет некорректный формат")
        return cls(
            profile=profile,
            phase=phase,
            worker_running=bool(payload["worker_running"]),
            busy=bool(payload["busy"]),
            current_task=current_task,
            operation_id=operation_id,
            session_id=session_id,
            handover_requested=bool(payload["handover_requested"]),
            draining=bool(payload["draining"]),
            stop_requested=bool(payload["stop_requested"]),
            terminal_state=terminal_state,
            worker_pid=worker_pid,
            worker_created_at=worker_created_at,
            updated_at=updated_at,
            freshness=freshness,
            provenance=provenance,
        )


def _profile(value: object) -> str:
    if not isinstance(value, str) or _SAFE_PROFILE.fullmatch(value) is None:
        raise RuntimeStateError("RUNTIME_PROFILE_INVALID", "Имя runtime-профиля имеет недопустимый формат")
    return value


def _token(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _SAFE_TOKEN.fullmatch(value) is None or ".." in value:
        raise RuntimeStateError("RUNTIME_STATE_ID_INVALID", f"{field} имеет недопустимый формат")
    return value


def _optional_token(value: object) -> str | None:
    if value is None:
        return None
    return _token(value, field="Идентификатор runtime operation")


def _optional_text(value: object, *, maximum: int) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise RuntimeStateError("RUNTIME_STATE_TEXT_INVALID", "Поле runtime state имеет недопустимый формат")
    return value


def _timestamp(value: object) -> str:
    if not isinstance(value, str) or len(value) > 80:
        raise RuntimeStateError("RUNTIME_STATE_TIMESTAMP_INVALID", "Метка времени runtime state имеет неверный формат")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RuntimeStateError(
            "RUNTIME_STATE_TIMESTAMP_INVALID", "Метка времени runtime state не является ISO timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise RuntimeStateError(
            "RUNTIME_STATE_TIMESTAMP_INVALID", "Метка времени runtime state должна быть в UTC"
        )
    return value


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _freshness(updated_at: str) -> str:
    try:
        age = datetime.now(UTC).timestamp() - datetime.fromisoformat(updated_at).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return "unknown"
    if -_FRESHNESS_SECONDS <= age <= _FRESHNESS_SECONDS:
        return "fresh"
    return "stale"


def _scoped_path(root: Path, relative: str) -> Path:
    root = Path(root).resolve()
    if not isinstance(relative, str):
        raise RuntimeStateError(
            "RUNTIME_STATE_PATH_INVALID",
            "Runtime state path должен быть относительной строкой",
        )
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise RuntimeStateError(
            "RUNTIME_STATE_PATH_INVALID",
            "Runtime state path не должен содержать абсолютный путь или переход к родительскому каталогу",
        )
    candidate = (root / relative_path).absolute()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise RuntimeStateError("RUNTIME_STATE_PATH_INVALID", "Runtime state path выходит за рабочую копию") from exc
    current = root
    for component in candidate.relative_to(root).parts:
        current /= component
        if current.is_symlink() or bool(getattr(current, "is_junction", lambda: False)()):
            raise RuntimeStateError("RUNTIME_STATE_UNSAFE_PATH", "Runtime state path проходит через ссылку или junction")
    return candidate


def _default_record(profile: str) -> dict[str, object]:
    return {
        "schema_version": _STATE_SCHEMA_VERSION,
        "profile": profile,
        "phase": RuntimePhase.STOPPED.value,
        "worker_running": False,
        "busy": False,
        "current_task": None,
        "operation_id": None,
        "session_id": None,
        "handover_requested": False,
        "draining": False,
        "stop_requested": False,
        "terminal_state": None,
        "worker_pid": None,
        "worker_created_at": None,
        "updated_at": _now(),
        "freshness": "fresh",
        "provenance": "unknown",
    }


class RuntimeStateStore:
    """Атомарное repository-scoped хранилище typed runtime snapshots."""

    def __init__(self, repository_root: Path | str, *, now: Callable[[], str] | None = None) -> None:
        self.repository_root = Path(repository_root).resolve()
        self.path = _scoped_path(self.repository_root, "config/state/webui-runtime-state.json")
        self.lock_path = _scoped_path(self.repository_root, "config/state/webui-runtime-state.lock")
        self._now = now or _now

    def read(self, profile: str) -> RuntimeStateSnapshot | None:
        profile = _profile(profile)
        payload = self._read_payload()
        records = payload.get("profiles", {})
        record = records.get(profile)
        if record is None:
            return None
        snapshot = RuntimeStateSnapshot.from_dict(record)
        if snapshot.profile != profile:
            raise RuntimeStateError(
                "RUNTIME_STATE_CORRUPT",
                "Ключ runtime-профиля не совпадает с записью snapshot",
            )
        return replace(snapshot, freshness=_freshness(snapshot.updated_at))

    def read_all(self) -> dict[str, RuntimeStateSnapshot]:
        payload = self._read_payload()
        records = payload.get("profiles", {})
        result: dict[str, RuntimeStateSnapshot] = {}
        for profile, record in records.items():
            profile = _profile(profile)
            snapshot = RuntimeStateSnapshot.from_dict(record)
            if snapshot.profile != profile:
                raise RuntimeStateError(
                    "RUNTIME_STATE_CORRUPT",
                    "Ключ runtime-профиля не совпадает с записью snapshot",
                )
            result[profile] = replace(snapshot, freshness=_freshness(snapshot.updated_at))
        return result

    def reconcile_with_authoritative_workers(
        self,
        authoritative_workers: Mapping[str, Mapping[str, object]],
    ) -> bool:
        """Восстановить эфемерный snapshot после несовместимого обновления.

        Runtime state не является источником scheduler/config данных. Единственным
        источником текущей identity worker является owner-specific registry, который
        передаётся вызывающей стороной. Поэтому несовместимый или структурно
        повреждённый snapshot можно атомарно сбросить только когда registry
        подтверждает отсутствие worker'ов. При наличии worker'ов сохранение старого
        файла и fail-closed безопаснее попытки угадать их phase/session.
        """

        workers = self._normalize_authoritative_workers(authoritative_workers)
        with application_host_lock(self.lock_path):
            try:
                payload = self._read_payload()
                for profile, record in payload["profiles"].items():
                    profile = _profile(profile)
                    snapshot = RuntimeStateSnapshot.from_dict(record)
                    if snapshot.profile != profile:
                        raise RuntimeStateError(
                            "RUNTIME_STATE_CORRUPT",
                            "Ключ runtime-профиля не совпадает с записью snapshot",
                        )
            except RuntimeStateError as error:
                if error.code not in {
                    "RUNTIME_STATE_SCHEMA_MISMATCH",
                    "RUNTIME_STATE_CORRUPT",
                }:
                    raise
                if workers:
                    raise RuntimeStateError(
                        error.code,
                        "Несовместимый runtime state нельзя безопасно восстановить при работающих worker",
                        details={
                            "recovery": "blocked",
                            "authoritative_worker_profiles": sorted(workers),
                            "cause_code": error.code,
                        },
                    ) from error
                _atomic_state_write(self.path, {})
                return True
        return False

    def mark_worker_started(
        self,
        profile: str,
        *,
        worker_pid: int,
        worker_created_at: float,
        operation_id: str | None = None,
        session_id: str | None = None,
        phase: RuntimePhase = RuntimePhase.USER_PROFILE_IDLE,
        provenance: str = "webui_owner",
    ) -> RuntimeStateSnapshot:
        self._validate_worker(worker_pid, worker_created_at)
        return self._update(
            profile,
            phase=phase,
            worker_running=True,
            busy=False,
            current_task=None,
            operation_id=operation_id,
            session_id=session_id,
            handover_requested=False,
            draining=False,
            stop_requested=False,
            terminal_state=None,
            worker_pid=worker_pid,
            worker_created_at=float(worker_created_at),
            provenance=provenance,
        )

    def mark_worker_stopped(
        self,
        profile: str,
        *,
        expected_worker_pid: int | None = None,
        expected_worker_created_at: float | None = None,
        operation_id: str | None = None,
        session_id: str | None = None,
        phase: RuntimePhase = RuntimePhase.STOPPED,
        terminal_state: str | None = "stopped",
        provenance: str = "webui_owner",
    ) -> RuntimeStateSnapshot:
        if (expected_worker_pid is None) != (expected_worker_created_at is None):
            raise RuntimeStateError(
                "RUNTIME_WORKER_ID_INVALID",
                "Ожидаемая identity worker должна содержать PID и created_at",
            )
        if expected_worker_pid is not None and expected_worker_created_at is not None:
            self._validate_worker(expected_worker_pid, expected_worker_created_at)
        return self._update(
            profile,
            phase=phase,
            worker_running=False,
            busy=False,
            current_task=None,
            operation_id=operation_id,
            session_id=session_id,
            handover_requested=False,
            draining=False,
            stop_requested=False,
            terminal_state=terminal_state,
            worker_pid=None,
            worker_created_at=None,
            provenance=provenance,
            _expected_worker=(expected_worker_pid, expected_worker_created_at)
            if expected_worker_pid is not None and expected_worker_created_at is not None
            else None,
            _expected_runtime=(operation_id, session_id)
            if expected_worker_pid is not None and expected_worker_created_at is not None
            else None,
        )

    def mark_task_started(self, profile: str, task: str, *, operation_id: str | None = None, session_id: str | None = None) -> RuntimeStateSnapshot:
        task = _optional_text(task, maximum=_MAX_TASK)
        if task is None:
            raise RuntimeStateError("RUNTIME_STATE_TEXT_INVALID", "Текстовое поле runtime state не может быть пустым")
        return self._update(
            profile,
            phase=RuntimePhase.USER_PROFILE_BUSY,
            worker_running=True,
            busy=True,
            current_task=task,
            operation_id=operation_id,
            session_id=session_id,
            handover_requested=False,
            draining=False,
            stop_requested=False,
            terminal_state=None,
            provenance="task_lifecycle",
            _preserve_handover=True,
        )

    def try_mark_task_started(
        self,
        profile: str,
        task: str,
        *,
        operation_id: str | None = None,
        session_id: str | None = None,
    ) -> bool:
        """Атомарно подтвердить границу задачи, если handover ещё не начат."""

        task = _optional_text(task, maximum=_MAX_TASK)
        if task is None:
            raise RuntimeStateError("RUNTIME_STATE_TEXT_INVALID", "Текстовое поле runtime state не может быть пустым")
        profile = _profile(profile)
        with application_host_lock(self.lock_path):
            current = self.read(profile)
            if (
                current is None
                or current.freshness != "fresh"
                or current.worker_running is not True
                or current.busy
                or current.handover_requested
                or current.draining
                or current.stop_requested
            ):
                return False
            self.mark_task_started(
                profile,
                task,
                operation_id=operation_id,
                session_id=session_id,
            )
            return True

    def mark_task_finished(self, profile: str, *, operation_id: str | None = None, session_id: str | None = None) -> RuntimeStateSnapshot:
        return self._update(
            profile,
            phase=RuntimePhase.USER_PROFILE_IDLE,
            busy=False,
            current_task=None,
            operation_id=operation_id,
            session_id=session_id,
            terminal_state=None,
            provenance="task_lifecycle",
            _preserve_handover=True,
        )

    def begin_handover(
        self,
        profile: str,
        *,
        operation_id: str,
        session_id: str | None = None,
        timeout_seconds: float = HOST_LOCK_TIMEOUT_SECONDS,
    ) -> RuntimeStateSnapshot | None:
        """Атомарно прочитать свежий worker и выставить handover-флаг."""

        profile = _profile(profile)
        operation_id = _token(operation_id, field="Идентификатор runtime operation")
        session_id = _optional_token(session_id)
        if (
            type(timeout_seconds) not in (int, float)
            or not math.isfinite(float(timeout_seconds))
            or not 0 < float(timeout_seconds) <= 120
        ):
            raise ValueError("Тайм-аут handover state должен быть в диапазоне (0, 120]")
        with application_host_lock(self.lock_path, timeout=float(timeout_seconds)):
            payload = self._read_payload()
            records = payload["profiles"]
            existing = records.get(profile)
            if existing is None:
                return None
            snapshot = RuntimeStateSnapshot.from_dict(existing)
            if snapshot.profile != profile:
                raise RuntimeStateError(
                    "RUNTIME_STATE_CORRUPT",
                    "Ключ runtime-профиля не совпадает с записью snapshot",
                )
            snapshot = replace(snapshot, freshness=_freshness(snapshot.updated_at))
            if snapshot.freshness != "fresh":
                raise RuntimeStateError(
                    "RUNTIME_HANDOVER_STATE_STALE",
                    "Состояние пользовательского профиля устарело",
                )
            if snapshot.worker_running is not True:
                return snapshot
            if snapshot.handover_requested and snapshot.operation_id not in (None, operation_id):
                raise RuntimeStateError(
                    "RUNTIME_HANDOVER_IN_PROGRESS",
                    "Для пользовательского профиля уже выполняется handover",
                )
            current = dict(snapshot.as_dict())
            current.update(
                {
                    "phase": RuntimePhase.HANDOVER_REQUESTED.value,
                    "operation_id": operation_id,
                    "session_id": session_id,
                    "handover_requested": True,
                    "updated_at": self._now(),
                    "freshness": "fresh",
                }
            )
            updated = RuntimeStateSnapshot.from_dict(current)
            records = dict(records)
            records[profile] = updated.as_dict()
            _atomic_state_write(self.path, records)
            return updated

    def request_handover(self, profile: str, *, operation_id: str, session_id: str | None = None) -> RuntimeStateSnapshot:
        return self._update(profile, phase=RuntimePhase.HANDOVER_REQUESTED, operation_id=operation_id, session_id=session_id, handover_requested=True)

    def mark_preemption_notice(self, profile: str, *, operation_id: str, session_id: str | None = None) -> RuntimeStateSnapshot:
        return self._update(profile, phase=RuntimePhase.PREEMPTION_NOTICE, operation_id=operation_id, session_id=session_id, handover_requested=True)

    def mark_grace_period(self, profile: str, *, operation_id: str, session_id: str | None = None) -> RuntimeStateSnapshot:
        return self._update(profile, phase=RuntimePhase.GRACE_PERIOD, operation_id=operation_id, session_id=session_id, handover_requested=True)

    def request_quiesce(self, profile: str, *, operation_id: str, session_id: str | None = None) -> RuntimeStateSnapshot:
        return self._update(profile, phase=RuntimePhase.QUIESCE_REQUESTED, operation_id=operation_id, session_id=session_id, handover_requested=True, stop_requested=True)

    def mark_draining(self, profile: str, *, operation_id: str, session_id: str | None = None) -> RuntimeStateSnapshot:
        return self._update(profile, phase=RuntimePhase.CURRENT_TASK_DRAINING, operation_id=operation_id, session_id=session_id, handover_requested=True, draining=True, stop_requested=True)

    def mark_current_task_stopped(self, profile: str, *, operation_id: str, session_id: str | None = None) -> RuntimeStateSnapshot:
        return self._update(profile, phase=RuntimePhase.CURRENT_TASK_STOPPED, worker_running=False, busy=False, current_task=None, operation_id=operation_id, session_id=session_id, handover_requested=True, draining=False, stop_requested=False, terminal_state="stopped", worker_pid=None, worker_created_at=None)

    def mark_returning_to_main(self, profile: str, *, operation_id: str, session_id: str | None = None) -> RuntimeStateSnapshot:
        return self._update(profile, phase=RuntimePhase.RETURNING_TO_MAIN, operation_id=operation_id, session_id=session_id, handover_requested=True, terminal_state=None)

    def mark_main_confirmed(self, profile: str, *, operation_id: str, session_id: str | None = None) -> RuntimeStateSnapshot:
        return self._update(profile, phase=RuntimePhase.MAIN_CONFIRMED, operation_id=operation_id, session_id=session_id, handover_requested=True, terminal_state="main_confirmed")

    def mark_resource_acquiring(self, profile: str, *, operation_id: str, session_id: str | None = None) -> RuntimeStateSnapshot:
        return self._update(profile, phase=RuntimePhase.RESOURCE_ACQUIRING, operation_id=operation_id, session_id=session_id, handover_requested=False, terminal_state=None)

    def mark_resource_ready(self, profile: str, *, worker_pid: int, worker_created_at: float, operation_id: str, session_id: str | None = None) -> RuntimeStateSnapshot:
        self._validate_worker(worker_pid, worker_created_at)
        return self._update(profile, phase=RuntimePhase.RESOURCE_READY, worker_running=True, busy=False, current_task=None, operation_id=operation_id, session_id=session_id, handover_requested=False, draining=False, stop_requested=False, terminal_state="ready", worker_pid=worker_pid, worker_created_at=float(worker_created_at), provenance="dev_runtime")

    def mark_failed(
        self,
        profile: str,
        *,
        operation_id: str | None = None,
        session_id: str | None = None,
        terminal_state: str = "failed",
        preserve_handover_flags: bool = False,
    ) -> RuntimeStateSnapshot:
        terminal_state = _optional_text(terminal_state, maximum=_MAX_TEXT)
        if terminal_state is None:
            raise RuntimeStateError("RUNTIME_STATE_TEXT_INVALID", "Текстовое поле runtime state не может быть пустым")
        return self._update(
            profile,
            phase=RuntimePhase.FAILED,
            operation_id=operation_id,
            session_id=session_id,
            handover_requested=False,
            draining=False,
            stop_requested=False,
            terminal_state=terminal_state,
            provenance="runtime_control",
            _preserve_handover_flags=preserve_handover_flags,
        )

    def _update(self, profile: str, **changes: object) -> RuntimeStateSnapshot:
        profile = _profile(profile)
        preserve_handover = changes.pop("_preserve_handover", False) is True
        preserve_handover_flags = changes.pop("_preserve_handover_flags", False) is True
        expected_worker = changes.pop("_expected_worker", None)
        expected_runtime = changes.pop("_expected_runtime", None)
        with application_host_lock(self.lock_path):
            payload = self._read_payload()
            records = dict(payload["profiles"])
            current = _default_record(profile)
            existing = records.get(profile)
            if existing is not None:
                existing_snapshot = RuntimeStateSnapshot.from_dict(existing)
                if existing_snapshot.profile != profile:
                    raise RuntimeStateError(
                        "RUNTIME_STATE_CORRUPT",
                        "Ключ runtime-профиля не совпадает с записью snapshot",
                    )
                current = dict(existing_snapshot.as_dict())
                if expected_worker is not None:
                    expected_pid, expected_created_at = expected_worker
                    if not existing_snapshot.worker_running:
                        return replace(existing_snapshot, freshness=_freshness(existing_snapshot.updated_at))
                    if (
                        existing_snapshot.worker_pid != expected_pid
                        or existing_snapshot.worker_created_at != expected_created_at
                    ):
                        raise RuntimeStateError(
                            "RUNTIME_STATE_STALE_WRITE",
                            "Попытка старого worker изменить snapshot нового worker отклонена",
                        )
                    if expected_runtime is not None:
                        expected_operation_id, expected_session_id = expected_runtime
                        if (
                            existing_snapshot.operation_id != expected_operation_id
                            or existing_snapshot.session_id != expected_session_id
                        ):
                            raise RuntimeStateError(
                                "RUNTIME_STATE_STALE_WRITE",
                                "Попытка устаревшего worker изменить snapshot другой runtime operation отклонена",
                            )
            elif expected_worker is not None:
                raise RuntimeStateError(
                    "RUNTIME_STATE_STALE_WRITE",
                    "Snapshot ожидаемого worker отсутствует",
                )
            if preserve_handover:
                for key in ("handover_requested", "draining", "stop_requested"):
                    changes[key] = current[key]
                for key in ("operation_id", "session_id"):
                    if changes.get(key) is None:
                        changes[key] = current[key]
                if current["handover_requested"]:
                    changes["phase"] = current["phase"]
            elif preserve_handover_flags and (
                current["handover_requested"]
                or current["draining"]
                or current["stop_requested"]
            ):
                for key in ("handover_requested", "draining", "stop_requested", "operation_id", "session_id"):
                    changes[key] = current[key]
            current.update(changes)
            current["profile"] = profile
            current["schema_version"] = _STATE_SCHEMA_VERSION
            current["updated_at"] = self._now()
            current["freshness"] = "fresh"
            records[profile] = current
            if len(records) > _MAX_PROFILES:
                raise RuntimeStateError("RUNTIME_STATE_LIMIT", "Число runtime-профилей превышает предел")
            snapshot = RuntimeStateSnapshot.from_dict(current)
            records[profile] = snapshot.as_dict()
            _atomic_state_write(self.path, records)
        return snapshot

    def _read_payload(self) -> dict[str, object]:
        try:
            if self.path.is_symlink() or bool(getattr(self.path, "is_junction", lambda: False)()):
                raise RuntimeStateError("RUNTIME_STATE_UNSAFE_PATH", "Runtime state файл не должен быть ссылкой")
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return {"schema_version": _STATE_SCHEMA_VERSION, "profiles": {}}
        except RuntimeStateError:
            raise
        except OSError as exc:
            raise RuntimeStateError("RUNTIME_STATE_UNREADABLE", "Runtime state невозможно прочитать") from exc
        if len(raw) > _MAX_STATE_BYTES:
            raise RuntimeStateError("RUNTIME_STATE_TOO_LARGE", "Runtime state превышает допустимый размер")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise RuntimeStateError("RUNTIME_STATE_CORRUPT", "Runtime state содержит некорректный JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeStateError("RUNTIME_STATE_CORRUPT", "Runtime state имеет неизвестную структуру")
        if payload.get("schema_version") != _STATE_SCHEMA_VERSION:
            if isinstance(payload.get("schema_version"), int) and not isinstance(
                payload.get("schema_version"), bool
            ):
                raise RuntimeStateError(
                    "RUNTIME_STATE_SCHEMA_MISMATCH",
                    "Runtime state сохранён в несовместимой schema version",
                    details={"persisted_schema_version": payload.get("schema_version")},
                )
            raise RuntimeStateError("RUNTIME_STATE_CORRUPT", "Runtime state имеет неизвестную структуру")
        if set(payload) != {"schema_version", "profiles"}:
            raise RuntimeStateError("RUNTIME_STATE_CORRUPT", "Runtime state имеет неизвестную структуру")
        profiles = payload.get("profiles")
        if not isinstance(profiles, dict) or len(profiles) > _MAX_PROFILES:
            raise RuntimeStateError("RUNTIME_STATE_CORRUPT", "Runtime state содержит некорректный каталог профилей")
        return {"schema_version": _STATE_SCHEMA_VERSION, "profiles": profiles}

    @staticmethod
    def _validate_worker(worker_pid: int, worker_created_at: float) -> None:
        if isinstance(worker_pid, bool) or not isinstance(worker_pid, int) or worker_pid <= 0:
            raise RuntimeStateError("RUNTIME_WORKER_ID_INVALID", "worker_pid должен быть положительным int")
        if isinstance(worker_created_at, bool) or not isinstance(worker_created_at, (int, float)) or not math.isfinite(float(worker_created_at)) or float(worker_created_at) <= 0:
            raise RuntimeStateError("RUNTIME_WORKER_ID_INVALID", "worker_created_at должен быть положительным числом")

    @classmethod
    def _normalize_authoritative_workers(
        cls,
        workers: Mapping[str, Mapping[str, object]],
    ) -> dict[str, dict[str, object]]:
        if not isinstance(workers, Mapping):
            raise RuntimeStateError(
                "RUNTIME_STATE_RECONCILIATION_REQUIRED",
                "Authoritative worker registry имеет неподдерживаемый формат",
            )
        normalized: dict[str, dict[str, object]] = {}
        for raw_profile, raw_record in workers.items():
            profile = _profile(raw_profile)
            if not isinstance(raw_record, Mapping):
                raise RuntimeStateError(
                    "RUNTIME_STATE_RECONCILIATION_REQUIRED",
                    "Authoritative worker registry содержит неподтверждённую identity",
                    details={"profile": profile},
                )
            if set(raw_record) != {"pid", "created_at"}:
                raise RuntimeStateError(
                    "RUNTIME_STATE_RECONCILIATION_REQUIRED",
                    "Authoritative worker registry содержит неизвестные поля identity",
                    details={"profile": profile},
                )
            pid = raw_record.get("pid")
            created_at = raw_record.get("created_at")
            try:
                cls._validate_worker(pid, created_at)
            except RuntimeStateError as error:
                raise RuntimeStateError(
                    "RUNTIME_STATE_RECONCILIATION_REQUIRED",
                    "Authoritative worker registry содержит некорректную identity",
                    details={"profile": profile, "cause_code": error.code},
                ) from error
            normalized[profile] = {
                "pid": pid,
                "created_at": float(created_at),
            }
        return normalized


def _atomic_state_write(path: Path, records: Mapping[str, object]) -> None:
    payload = {"schema_version": _STATE_SCHEMA_VERSION, "profiles": dict(records)}
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    if len(encoded.encode("utf-8")) > _MAX_STATE_BYTES:
        raise RuntimeStateError("RUNTIME_STATE_TOO_LARGE", "Runtime state превышает допустимый размер")
    try:
        atomic_write(path, encoded)
    except OSError as exc:
        raise RuntimeStateError("RUNTIME_STATE_WRITE_FAILED", "Runtime state невозможно записать") from exc


__all__ = [
    "RuntimePhase",
    "RuntimeStateError",
    "RuntimeStateSnapshot",
    "RuntimeStateStore",
]
