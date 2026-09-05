"""Координатор cooperative handover пользовательского профиля в development profile."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic, sleep
from typing import Protocol

from module.application.runtime_state import (
    RuntimePhase,
    RuntimeStateError,
    RuntimeStateSnapshot,
)


class NotificationOutcome(StrEnum):
    """Подтверждённый результат передачи уведомления в WebUI."""

    ACCEPTED = "accepted"
    DELIVERED = "delivered"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class HandoverPolicy:
    """Ограниченная policy без task-specific значений в coordinator."""

    grace_period_seconds: float = 30.0
    quiesce_timeout_seconds: float = 30.0
    poll_seconds: float = 0.1

    def __post_init__(self) -> None:
        for name in ("grace_period_seconds", "quiesce_timeout_seconds", "poll_seconds"):
            value = getattr(self, name)
            if (
                type(value) not in (int, float)
                or not math.isfinite(float(value))
                or value < 0
            ):
                raise ValueError(f"{name} должен быть неотрицательным числом")
        if self.grace_period_seconds > 300 or self.quiesce_timeout_seconds > 300:
            raise ValueError("Тайм-аут handover не должен превышать 300 секунд")
        if not 0 < self.poll_seconds <= 5:
            raise ValueError("poll_seconds должен быть в диапазоне (0, 5]")


class HandoverHooks(Protocol):
    def begin_handover(
        self,
        profile: str,
        operation_id: str,
        session_id: str | None,
    ) -> RuntimeStateSnapshot | None: ...

    def read_state(self, profile: str) -> RuntimeStateSnapshot | None: ...

    def mark_phase(self, profile: str, phase: RuntimePhase, operation_id: str, session_id: str | None) -> None: ...

    def notify_preemption(
        self,
        profile: str,
        operation_id: str,
        session_id: str | None,
    ) -> NotificationOutcome: ...

    def request_cooperative_quiesce(self, profile: str, operation_id: str, session_id: str | None) -> bool: ...

    def wait_worker_stopped(self, profile: str, timeout_seconds: float) -> bool: ...

    def return_to_main(self, profile: str, operation_id: str, session_id: str | None) -> bool: ...

    def is_main_confirmed(self, profile: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class HandoverResult:
    ok: bool
    code: str
    message: str
    profile: str
    operation_id: str
    phases: tuple[str, ...]
    details: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "code": self.code,
            "message": self.message,
            "profile": self.profile,
            "operation_id": self.operation_id,
            "phases": list(self.phases),
            "details": dict(self.details),
        }


class ProfileHandoverCoordinator:
    """Выполнить handover, не используя hard ``ProcessManager.stop``."""

    def __init__(
        self,
        policy: HandoverPolicy | None = None,
        *,
        monotonic_clock: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self.policy = policy or HandoverPolicy()
        self._monotonic = monotonic_clock or monotonic
        self._sleep = sleep_fn or sleep

    def run(
        self,
        profile: str,
        *,
        operation_id: str,
        session_id: str | None,
        hooks: HandoverHooks,
        deadline_check: Callable[[], bool] | None = None,
        deadline_remaining: Callable[[], float] | None = None,
    ) -> HandoverResult:
        phases: list[str] = []
        if deadline_check is not None and deadline_check() is not True:
            return HandoverResult(
                False,
                "RUNTIME_CONTROL_EXPIRED",
                "Срок действия runtime control request истёк до начала handover",
                profile,
                operation_id,
                tuple(phases),
                {"busy": None},
            )
        try:
            initial = hooks.begin_handover(profile, operation_id, session_id)
        except RuntimeStateError as exc:
            return HandoverResult(
                False,
                exc.code,
                str(exc),
                profile,
                operation_id,
                tuple(phases),
                {"busy": None},
            )
        except TimeoutError:
            return HandoverResult(
                False,
                "RUNTIME_CONTROL_EXPIRED"
                if deadline_check is not None and deadline_check() is not True
                else "RUNTIME_HANDOVER_STATE_UNKNOWN",
                "Не удалось получить bounded handover state в ограниченный срок",
                profile,
                operation_id,
                tuple(phases),
                {"busy": None},
            )
        if initial is None:
            return HandoverResult(
                False,
                "RUNTIME_HANDOVER_STATE_UNKNOWN",
                "Состояние пользовательского профиля не подтверждено; handover остановлен",
                profile,
                operation_id,
                tuple(phases),
                {"busy": None},
            )
        if initial.freshness != "fresh":
            return HandoverResult(
                False,
                "RUNTIME_HANDOVER_STATE_STALE",
                "Состояние пользовательского профиля устарело; handover остановлен",
                profile,
                operation_id,
                tuple(phases),
                {"busy": None, "freshness": initial.freshness},
            )
        if initial.worker_running is not True:
            return HandoverResult(
                True,
                "RUNTIME_HANDOVER_NOT_REQUIRED",
                "Рабочий процесс профиля уже остановлен",
                profile,
                operation_id,
                tuple(phases),
                {"busy": False},
            )

        handover_details: dict[str, object] = {"busy": initial.busy}
        phases.append(RuntimePhase.HANDOVER_REQUESTED.value)
        if initial.busy:
            if deadline_check is not None and deadline_check() is not True:
                return self._fail(
                    hooks,
                    profile,
                    operation_id,
                    session_id,
                    phases,
                    "RUNTIME_CONTROL_EXPIRED",
                    "Срок действия runtime control request истёк во время handover",
                    details=handover_details,
                )
            mark_failure = self._mark(
                hooks, profile, RuntimePhase.PREEMPTION_NOTICE, operation_id, session_id, phases
            )
            if mark_failure is not None:
                return mark_failure
            if deadline_check is not None and deadline_check() is not True:
                return self._fail(
                    hooks,
                    profile,
                    operation_id,
                    session_id,
                    phases,
                    "RUNTIME_CONTROL_EXPIRED",
                    "Срок действия runtime control request истёк до уведомления",
                    details=handover_details,
                )
            notification = hooks.notify_preemption(profile, operation_id, session_id)
            if deadline_check is not None and deadline_check() is not True:
                return self._fail(
                    hooks,
                    profile,
                    operation_id,
                    session_id,
                    phases,
                    "RUNTIME_CONTROL_EXPIRED",
                    "Срок действия runtime control request истёк после уведомления",
                    details=handover_details,
                )
            if notification is not NotificationOutcome.DELIVERED:
                outcome = notification.value if isinstance(notification, NotificationOutcome) else NotificationOutcome.FAILED.value
                return self._fail(
                    hooks,
                    profile,
                    operation_id,
                    session_id,
                    phases,
                    "RUNTIME_HANDOVER_NOTIFICATION_FAILED",
                    "Пользователь не был подтверждённо уведомлён о вытеснении; handover остановлен",
                    details={
                        **handover_details,
                        "notification": {
                            "attempted": True,
                            "outcome": outcome,
                            "confirmed": False,
                        },
                    },
                )
            handover_details["notification"] = {
                "attempted": True,
                "outcome": NotificationOutcome.DELIVERED.value,
                "confirmed": True,
            }
            mark_failure = self._mark(
                hooks, profile, RuntimePhase.GRACE_PERIOD, operation_id, session_id, phases
            )
            if mark_failure is not None:
                return mark_failure
            busy_state = self._wait_until_not_busy(profile, hooks, deadline_check=deadline_check)
            if busy_state == "expired":
                return self._fail(
                    hooks,
                    profile,
                    operation_id,
                    session_id,
                    phases,
                    "RUNTIME_CONTROL_EXPIRED",
                    "Срок действия runtime control request истёк во время grace period",
                    details=handover_details,
                )
            if busy_state is None:
                return self._fail(
                    hooks,
                    profile,
                    operation_id,
                    session_id,
                    phases,
                    "RUNTIME_HANDOVER_STATE_UNKNOWN",
                    "Состояние текущего task потеряно во время grace period; development profile не запускается",
                    details=handover_details,
                )
            handover_details["grace_period"] = {"expired": busy_state is False}

        if deadline_check is not None and deadline_check() is not True:
            return self._fail(
                hooks,
                profile,
                operation_id,
                session_id,
                phases,
                "RUNTIME_CONTROL_EXPIRED",
                "Срок действия runtime control request истёк до cooperative quiesce",
                details=handover_details,
            )
        mark_failure = self._mark(
            hooks, profile, RuntimePhase.QUIESCE_REQUESTED, operation_id, session_id, phases
        )
        if mark_failure is not None:
            return mark_failure
        if hooks.request_cooperative_quiesce(profile, operation_id, session_id) is not True:
            return self._fail(
                hooks,
                profile,
                operation_id,
                session_id,
                phases,
                "RUNTIME_HANDOVER_QUIESCE_FAILED",
                "Профиль не принял cooperative quiesce; handover остановлен",
                details=handover_details,
            )
        if deadline_check is not None and deadline_check() is not True:
            return self._fail(
                hooks,
                profile,
                operation_id,
                session_id,
                phases,
                "RUNTIME_CONTROL_EXPIRED",
                "Срок действия runtime control request истёк во время quiesce",
                details=handover_details,
            )
        mark_failure = self._mark(
            hooks, profile, RuntimePhase.CURRENT_TASK_DRAINING, operation_id, session_id, phases
        )
        if mark_failure is not None:
            return mark_failure
        wait_timeout = self.policy.quiesce_timeout_seconds
        if deadline_remaining is not None:
            try:
                remaining = float(deadline_remaining())
            except (TypeError, ValueError, OverflowError):
                remaining = 0.0
            if not math.isfinite(remaining) or remaining <= 0:
                return self._fail(
                    hooks,
                    profile,
                    operation_id,
                    session_id,
                    phases,
                    "RUNTIME_CONTROL_EXPIRED",
                    "Срок действия runtime control request истёк до ожидания остановки worker",
                    details=handover_details,
                )
            wait_timeout = min(wait_timeout, remaining)
        if hooks.wait_worker_stopped(profile, wait_timeout) is not True:
            return self._fail(
                hooks,
                profile,
                operation_id,
                session_id,
                phases,
                "RUNTIME_HANDOVER_TIMEOUT",
                "Профиль не завершил текущий task в ограниченный quiesce timeout; development profile не запускается",
                details=handover_details,
            )
        if deadline_check is not None and deadline_check() is not True:
            return self._fail(
                hooks,
                profile,
                operation_id,
                session_id,
                phases,
                "RUNTIME_CONTROL_EXPIRED",
                "Срок действия runtime control request истёк после остановки worker",
                details=handover_details,
            )
        mark_failure = self._mark(
            hooks, profile, RuntimePhase.CURRENT_TASK_STOPPED, operation_id, session_id, phases
        )
        if mark_failure is not None:
            return mark_failure
        mark_failure = self._mark(
            hooks, profile, RuntimePhase.RETURNING_TO_MAIN, operation_id, session_id, phases
        )
        if mark_failure is not None:
            return mark_failure
        if deadline_check is not None and deadline_check() is not True:
            return self._fail(
                hooks,
                profile,
                operation_id,
                session_id,
                phases,
                "RUNTIME_CONTROL_EXPIRED",
                "Срок действия runtime control request истёк до подтверждения главного экрана",
                details=handover_details,
            )
        if hooks.return_to_main(profile, operation_id, session_id) is not True:
            return self._fail(
                hooks,
                profile,
                operation_id,
                session_id,
                phases,
                "RUNTIME_HANDOVER_MAIN_FAILED",
                "Возврат пользовательского профиля на главный экран не подтверждён; development profile не запускается",
                details=handover_details,
            )
        if deadline_check is not None and deadline_check() is not True:
            return self._fail(
                hooks,
                profile,
                operation_id,
                session_id,
                phases,
                "RUNTIME_CONTROL_EXPIRED",
                "Срок действия runtime control request истёк после возврата на главный экран",
                details=handover_details,
            )
        if hooks.is_main_confirmed(profile) is not True:
            return self._fail(
                hooks,
                profile,
                operation_id,
                session_id,
                phases,
                "RUNTIME_HANDOVER_MAIN_UNCONFIRMED",
                "Главный экран пользовательского профиля не подтверждён; development profile не запускается",
                details=handover_details,
            )
        if deadline_check is not None and deadline_check() is not True:
            return self._fail(
                hooks,
                profile,
                operation_id,
                session_id,
                phases,
                "RUNTIME_CONTROL_EXPIRED",
                "Срок действия runtime control request истёк до фиксации главного экрана",
                details=handover_details,
            )
        mark_failure = self._mark(
            hooks, profile, RuntimePhase.MAIN_CONFIRMED, operation_id, session_id, phases
        )
        if mark_failure is not None:
            return mark_failure
        return HandoverResult(
            True,
            "RUNTIME_HANDOVER_READY",
            "Пользовательский профиль безопасно подготовлен к запуску development profile",
            profile,
            operation_id,
            tuple(phases),
            handover_details,
        )

    def _wait_until_not_busy(
        self,
        profile: str,
        hooks: HandoverHooks,
        *,
        deadline_check: Callable[[], bool] | None = None,
    ) -> bool | None | str:
        deadline = self._monotonic() + self.policy.grace_period_seconds
        while True:
            if deadline_check is not None and deadline_check() is not True:
                return "expired"
            state = hooks.read_state(profile)
            if state is None:
                return None
            if state.freshness != "fresh":
                return None
            if state.busy is not True:
                return True
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return False
            self._sleep(min(self.policy.poll_seconds, remaining))

    @staticmethod
    def _mark(
        hooks: HandoverHooks,
        profile: str,
        phase: RuntimePhase,
        operation_id: str,
        session_id: str | None,
        phases: list[str],
    ) -> HandoverResult | None:
        try:
            hooks.mark_phase(profile, phase, operation_id, session_id)
        except RuntimeStateError as exc:
            return ProfileHandoverCoordinator._fail(
                hooks,
                profile,
                operation_id,
                session_id,
                phases,
                exc.code,
                str(exc),
                details={"failed_phase": phase.value},
            )
        phases.append(phase.value)
        return None

    @staticmethod
    def _fail(
        hooks: HandoverHooks,
        profile: str,
        operation_id: str,
        session_id: str | None,
        phases: list[str],
        code: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> HandoverResult:
        failure_details = dict(details or {})
        try:
            hooks.mark_phase(profile, RuntimePhase.FAILED, operation_id, session_id)
            phases.append(RuntimePhase.FAILED.value)
        except Exception as exc:  # noqa: BLE001, S110 - терминальная ошибка остаётся в fail-closed режиме.
            # Исходная ошибка остаётся результатом проверки; best-effort запись
            # состояния не может превратить fail-closed операцию в успех.
            failure_details["failure_phase_recorded"] = False
            failure_details["failure_phase_error"] = type(exc).__name__
        return HandoverResult(
            False,
            code,
            message,
            profile,
            operation_id,
            tuple(phases),
            failure_details,
        )


__all__ = [
    "HandoverHooks",
    "HandoverPolicy",
    "HandoverResult",
    "NotificationOutcome",
    "ProfileHandoverCoordinator",
]
