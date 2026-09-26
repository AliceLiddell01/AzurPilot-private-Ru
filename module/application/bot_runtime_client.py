"""Клиент WebUI к независимому headless Bot Runtime."""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path

from module.application.runtime_control import (
    BotRuntimeBootstrapper,
    RuntimeControlClient,
    RuntimeControlError,
    RuntimeControlOperation,
    RUNTIME_CONTROL_PROFILE,
    RuntimeOwnerIdentity,
)
from module.application.runtime_state import RuntimePhase, RuntimeStateStore
from module.application.runtime_worker_registry import (
    ReadOnlyWorkerStatus,
    get_canonical_owner_record_read_only,
    get_canonical_workers_read_only,
    process_matches,
    read_canonical_worker_read_only,
)
from module.application.runtime_log_projection import (
    read_runtime_log_events,
    runtime_log_signature,
)
from module.config.utils import DEFAULT_CONFIG_NAME
from module.logger import logger


_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class _ProfileClient:
    """Клиент управления одним профилем; worker lifecycle исполняет Bot Runtime."""

    def __init__(self, config_name: str) -> None:
        self.config_name = config_name
        self.renderables: list[object] = []
        self.renderables_max_length = 2000
        self.renderables_reduce_length = 1000
        self.renderables_total = 0
        self.renderables_generation = 0
        self._projected_events: tuple[RuntimeLogEvent, ...] = ()
        self._log_signature: tuple[tuple[str, int, int] | tuple[str, None, None], ...] | None = None
        self._state_override: int | None = None
        self._state_override_deadline: float | None = None

    @property
    def alive(self) -> bool:
        return self.state == 1

    @property
    def state(self) -> int:
        override = self._get_state_override()
        if override is not None:
            return override
        try:
            result = read_canonical_worker_read_only(
                self.config_name, repository_root=_REPOSITORY_ROOT
            )
            if result.status is ReadOnlyWorkerStatus.UNKNOWN:
                return 3
            if result.status is ReadOnlyWorkerStatus.VERIFIED:
                matches = process_matches(result.record)
                if matches is True:
                    return 1
                if matches is False:
                    return 3
            snapshot = RuntimeStateStore(_REPOSITORY_ROOT).read(self.config_name)
            if snapshot is not None:
                if snapshot.phase is RuntimePhase.FAILED:
                    return 3
                if snapshot.worker_running:
                    worker = {
                        "pid": snapshot.worker_pid,
                        "created_at": snapshot.worker_created_at,
                    }
                    matches = process_matches(worker)
                    if matches is True:
                        return 1
                    if matches is False:
                        return 3
        except Exception:  # noqa: BLE001 - UI показывает неизвестный runtime как ошибку.
            return 3
        return 2

    def start(self, func: str | None, ev: object | None = None) -> None:
        del ev  # Event старого локального менеджера не пересекает typed runtime boundary.
        try:
            result = _control_client().call(
                RuntimeControlOperation.START_PROFILE,
                self.config_name,
                function=func,
            )
            if not result.ok:
                raise RuntimeControlError(result.code, result.message)
        except Exception as exc:  # noqa: BLE001 - WebUI callback должен отобразить отказ без local fallback.
            logger.error("[%s] Bot Runtime отклонил запуск профиля: %s", self.config_name, exc)

    def stop(self) -> bool:
        try:
            result = _control_client().call(
                RuntimeControlOperation.STOP_PROFILE,
                self.config_name,
            )
            if not result.ok:
                logger.error("[%s] Bot Runtime отклонил остановку профиля: %s", self.config_name, result.message)
                return False
            return not self.alive
        except Exception as exc:  # noqa: BLE001 - UI callback не должен напрямую завершать worker.
            logger.error("[%s] Не удалось остановить профиль через Bot Runtime: %s", self.config_name, exc)
            return False

    def set_state_override(self, state: int, duration: float = 10) -> None:
        if state not in (1, 2, 3):
            raise ValueError(f"Недопустимое переопределение состояния: {state}")
        self._state_override = state
        self._state_override_deadline = time.time() + duration if duration and duration > 0 else None

    def clear_state_override(self) -> None:
        self._state_override = None
        self._state_override_deadline = None

    @staticmethod
    def _projected_delta(
        previous: tuple[RuntimeLogEvent, ...],
        current: tuple[RuntimeLogEvent, ...],
    ) -> tuple[RuntimeLogEvent, ...] | None:
        """Вернуть новые события bounded tail или None при потере непрерывности.

        Оба снимка — хвосты одного журнала, который только дописывается и может
        обрезаться ротацией. Непрерывность подтверждает максимальное перекрытие
        ``previous[-overlap:] == current[:overlap]``: полное перекрытие — обычный
        append, неполное — ротация/compaction, а его отсутствие означает, что
        общий контекст доказать нельзя, и вызывающая сторона выполняет
        controlled rebuild вместо молчаливой потери событий.

        Перекрытие считается по префиксу нового снимка, поэтому повторяющиеся
        соседние события не теряются: ``(A,)`` против ``(A, A)`` даёт ровно один
        новый ``A``, а не пустую дельту.
        """

        if not previous:
            return current
        if current == previous:
            return ()
        overlap = min(len(previous), len(current))
        while overlap >= 1 and previous[-overlap:] != current[:overlap]:
            overlap -= 1
        if overlap < 1:
            return None
        return current[overlap:]

    def refresh_renderables(self) -> bool:
        try:
            signature = runtime_log_signature(
                self.config_name,
                repository_root=_REPOSITORY_ROOT,
            )
            if signature == self._log_signature:
                return False
            events = read_runtime_log_events(
                self.config_name,
                repository_root=_REPOSITORY_ROOT,
            )
        except (OSError, ValueError):
            return False

        self._log_signature = signature
        delta = self._projected_delta(self._projected_events, events)
        self._projected_events = events

        if delta is None:
            self.renderables = list(events)
            self.renderables_total += len(self.renderables)
            self.renderables_generation += 1
            return True
        if not delta:
            return False

        self.renderables.extend(delta)
        self.renderables_total += len(delta)
        if len(self.renderables) > self.renderables_max_length:
            del self.renderables[: self.renderables_reduce_length]
        return True

    def _get_state_override(self) -> int | None:
        if self._state_override is None:
            return None
        if self._state_override_deadline is not None and time.time() >= self._state_override_deadline:
            self.clear_state_override()
            return None
        return self._state_override


_client_lock = threading.Lock()
_profiles: dict[str, _ProfileClient] = {}
_runtime_control_client: RuntimeControlClient | None = None


def _owner_reader() -> RuntimeOwnerIdentity | None:
    record = get_canonical_owner_record_read_only(
        repository_root=_REPOSITORY_ROOT
    )
    return None if record is None else RuntimeOwnerIdentity.from_value(record)


def _owner_matches(owner: RuntimeOwnerIdentity) -> bool:
    try:
        return process_matches(owner.as_dict()) is True
    except RuntimeError:
        return False


def _control_client() -> RuntimeControlClient:
    global _runtime_control_client
    with _client_lock:
        if _runtime_control_client is None:
            _runtime_control_client = RuntimeControlClient(
                _REPOSITORY_ROOT,
                owner_reader=_owner_reader,
                owner_matches=_owner_matches,
                bootstrapper=BotRuntimeBootstrapper(
                    _REPOSITORY_ROOT,
                    owner_reader=_owner_reader,
                    owner_matches=_owner_matches,
                ),
            )
        return _runtime_control_client


class BotRuntimeClient:
    """Набор read-only статусов и typed lifecycle calls для WebUI."""

    @classmethod
    def get_manager(cls, config_name: str = DEFAULT_CONFIG_NAME) -> _ProfileClient:
        with _client_lock:
            return _profiles.setdefault(config_name, _ProfileClient(config_name))

    @classmethod
    def is_running(cls, config_name: str) -> bool:
        return cls.get_manager(config_name).alive

    @classmethod
    def running_instances(cls) -> list[_ProfileClient]:
        workers = get_canonical_workers_read_only(
            repository_root=_REPOSITORY_ROOT
        )
        names = []
        for name, record in workers.items():
            matches = process_matches(record)
            if matches is True:
                names.append(name)
            elif matches is False:
                raise RuntimeError(f"Identity worker профиля {name} не совпадает с текущим PID")
        return [cls.get_manager(name) for name in names]

    @classmethod
    def remove_manager(cls, config_name: str) -> None:
        with _client_lock:
            _profiles.pop(config_name, None)

    @classmethod
    def start_configured_profiles(cls) -> None:
        """Попросить Bot Runtime запустить настроенные профили при явном старте WebUI."""
        result = _control_client().call(
            RuntimeControlOperation.START_CONFIGURED_PROFILES,
            RUNTIME_CONTROL_PROFILE,
            idempotency_key=f"webui-start-{uuid.uuid4()}",
        )
        if not result.ok:
            raise RuntimeControlError(result.code, result.message)


# Совместимость со старыми импортами; этот объект только клиент и не владеет worker.
ProcessManager = BotRuntimeClient

__all__ = ["BotRuntimeClient", "ProcessManager"]
