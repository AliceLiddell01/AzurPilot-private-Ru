from __future__ import annotations

from collections import deque

import pytest

from module.application.errors import OperationFailedError
from module.application.runtime_handover import (
    HandoverHooks,
    HandoverPolicy,
    NotificationOutcome,
    ProfileHandoverCoordinator,
)
from module.application.runtime_state import (
    RuntimePhase,
    RuntimeStateError,
    RuntimeStateSnapshot,
)


def _snapshot(*, busy: bool, worker_running: bool = True) -> RuntimeStateSnapshot:
    return RuntimeStateSnapshot(
        profile="alas",
        phase=RuntimePhase.USER_PROFILE_BUSY if busy else RuntimePhase.USER_PROFILE_IDLE,
        worker_running=worker_running,
        busy=busy,
        current_task="DailyTask" if busy else None,
        operation_id="user-operation",
        session_id=None,
        handover_requested=False,
        draining=False,
        stop_requested=False,
        terminal_state=None,
        worker_pid=1001 if worker_running else None,
        worker_created_at=2001.0 if worker_running else None,
        updated_at="2026-09-04T00:00:00+00:00",
        freshness="fresh",
        provenance="task_lifecycle",
    )


class Hooks(HandoverHooks):
    def __init__(self, states: list[RuntimeStateSnapshot | None]) -> None:
        self.begin_states = deque(states[:1])
        self.read_states = deque(states[1:])
        self.begin_calls = 0
        self.read_calls = 0
        self.phases: list[str] = []
        self.notifications = 0
        self.notification_outcome = NotificationOutcome.DELIVERED
        self.quiesce_requests = 0
        self.wait_calls: list[float] = []
        self.return_calls = 0
        self.main_confirmed = True

    def begin_handover(
        self,
        profile: str,
        operation_id: str,
        session_id: str | None,
    ) -> RuntimeStateSnapshot | None:
        self.begin_calls += 1
        return self.begin_states.popleft() if self.begin_states else None

    def read_state(self, profile: str) -> RuntimeStateSnapshot | None:
        self.read_calls += 1
        return self.read_states.popleft() if self.read_states else None

    def mark_phase(self, profile: str, phase: RuntimePhase, operation_id: str, session_id: str | None) -> None:
        self.phases.append(phase.value)

    def notify_preemption(
        self,
        profile: str,
        operation_id: str,
        session_id: str | None,
    ) -> NotificationOutcome:
        self.notifications += 1
        return self.notification_outcome

    def request_cooperative_quiesce(self, profile: str, operation_id: str, session_id: str | None) -> bool:
        self.quiesce_requests += 1
        return True

    def wait_worker_stopped(self, profile: str, timeout_seconds: float) -> bool:
        self.wait_calls.append(timeout_seconds)
        return True

    def return_to_main(self, profile: str, operation_id: str, session_id: str | None) -> bool:
        self.return_calls += 1
        return True

    def is_main_confirmed(self, profile: str) -> bool:
        return self.main_confirmed


def _coordinator() -> ProfileHandoverCoordinator:
    return ProfileHandoverCoordinator(
        HandoverPolicy(grace_period_seconds=0, quiesce_timeout_seconds=3, poll_seconds=0.001),
        sleep_fn=lambda _seconds: None,
    )


def test_idle_handover_uses_cooperative_stop_and_confirms_main() -> None:
    hooks = Hooks([_snapshot(busy=False)])
    result = _coordinator().run(
        "alas",
        operation_id="handover-1",
        session_id="session-1",
        hooks=hooks,
    )

    assert result.ok is True
    assert hooks.begin_calls == 1
    assert hooks.read_calls == 0
    assert hooks.notifications == 0
    assert hooks.quiesce_requests == 1
    assert hooks.return_calls == 1
    assert hooks.phases == [
        "quiesce_requested",
        "current_task_draining",
        "current_task_stopped",
        "returning_to_main",
        "main_confirmed",
    ]
    assert result.phases[0] == "handover_requested"


def test_handover_converts_phase_mark_failure_to_fail_closed_result() -> None:
    hooks = Hooks([_snapshot(busy=False)])
    original_mark = hooks.mark_phase

    def fail_mark(
        profile: str,
        phase: RuntimePhase,
        operation_id: str,
        session_id: str | None,
    ) -> None:
        if phase is RuntimePhase.QUIESCE_REQUESTED:
            raise RuntimeStateError("RUNTIME_STATE_WRITE_FAILED", "синтетическая ошибка записи фазы")
        original_mark(profile, phase, operation_id, session_id)

    hooks.mark_phase = fail_mark  # type: ignore[method-assign]

    result = _coordinator().run(
        "alas",
        operation_id="handover-mark-failure",
        session_id="session-1",
        hooks=hooks,
    )

    assert result.ok is False
    assert result.code == "RUNTIME_STATE_WRITE_FAILED"
    assert result.message == "синтетическая ошибка записи фазы"
    assert result.details["failed_phase"] == "quiesce_requested"
    assert result.phases[-1] == "failed"
    assert hooks.phases[-1] == "failed"


def test_busy_handover_warns_before_grace_and_does_not_use_hard_stop() -> None:
    hooks = Hooks([_snapshot(busy=True), _snapshot(busy=False)])
    result = _coordinator().run(
        "alas",
        operation_id="handover-2",
        session_id=None,
        hooks=hooks,
    )

    assert result.ok is True
    assert hooks.begin_calls == 1
    assert hooks.read_calls == 1
    assert hooks.notifications == 1
    assert hooks.wait_calls == [3]
    assert hooks.phases[:2] == ["preemption_notice", "grace_period"]
    assert hooks.phases[-3:] == ["current_task_stopped", "returning_to_main", "main_confirmed"]
    assert result.phases[:3] == ("handover_requested", "preemption_notice", "grace_period")


def test_busy_handover_fails_closed_on_unknown_state_and_timeout() -> None:
    unknown_hooks = Hooks([_snapshot(busy=True), None])
    unknown = _coordinator().run(
        "alas",
        operation_id="handover-3",
        session_id=None,
        hooks=unknown_hooks,
    )
    assert unknown.ok is False
    assert unknown.code == "RUNTIME_HANDOVER_STATE_UNKNOWN"
    assert unknown_hooks.quiesce_requests == 0
    assert unknown_hooks.read_calls == 1

    timeout_hooks = Hooks([_snapshot(busy=False)])
    timeout_hooks.wait_worker_stopped = lambda _profile, _timeout: False
    timeout = _coordinator().run(
        "alas",
        operation_id="handover-4",
        session_id=None,
        hooks=timeout_hooks,
    )
    assert timeout.ok is False
    assert timeout.code == "RUNTIME_HANDOVER_TIMEOUT"
    assert timeout_hooks.return_calls == 0


def test_handover_requires_an_authoritative_initial_state() -> None:
    hooks = Hooks([None])
    result = _coordinator().run(
        "alas",
        operation_id="handover-5",
        session_id=None,
        hooks=hooks,
    )
    assert result.ok is False
    assert result.code == "RUNTIME_HANDOVER_STATE_UNKNOWN"


def test_handover_fails_closed_when_deadline_check_raises() -> None:
    hooks = Hooks([_snapshot(busy=False)])

    def broken_deadline() -> bool:
        raise RuntimeError("synthetic deadline failure")

    result = _coordinator().run(
        "alas",
        operation_id="handover-deadline-error",
        session_id="session-1",
        hooks=hooks,
        deadline_check=broken_deadline,
    )

    assert result.ok is False
    assert result.code == "RUNTIME_CONTROL_EXPIRED"
    assert hooks.begin_calls == 0


def test_handover_fails_closed_when_deadline_remaining_raises() -> None:
    hooks = Hooks([_snapshot(busy=False)])

    def broken_remaining() -> float:
        raise RuntimeError("synthetic deadline remaining failure")

    result = _coordinator().run(
        "alas",
        operation_id="handover-deadline-remaining-error",
        session_id="session-1",
        hooks=hooks,
        deadline_remaining=broken_remaining,
    )

    assert result.ok is False
    assert result.code == "RUNTIME_CONTROL_EXPIRED"
    assert hooks.wait_calls == []


def test_handover_requires_confirmed_notification_delivery() -> None:
    hooks = Hooks([_snapshot(busy=True)])
    hooks.notification_outcome = NotificationOutcome.ACCEPTED

    result = _coordinator().run(
        "alas",
        operation_id="handover-notification",
        session_id=None,
        hooks=hooks,
    )

    assert result.ok is False
    assert result.code == "RUNTIME_HANDOVER_NOTIFICATION_FAILED"
    assert hooks.quiesce_requests == 0
    assert result.details["notification"] == {
        "attempted": True,
        "outcome": "accepted",
        "confirmed": False,
    }


def _raise_unexpected_hook_error(*_args: object, **_kwargs: object) -> object:
    raise RuntimeError("синтетическая ошибка handover hook")


@pytest.mark.parametrize(
    ("hook_name", "busy", "failed_phase"),
    (
        ("notify_preemption", True, "preemption_notice"),
        ("read_state", True, "grace_period"),
        ("request_cooperative_quiesce", False, "quiesce_requested"),
        ("wait_worker_stopped", False, "current_task_draining"),
        ("return_to_main", False, "returning_to_main"),
        ("is_main_confirmed", False, "main_confirmed"),
    ),
)
def test_handover_converts_unexpected_hook_errors_to_fail_closed_result(
    hook_name: str,
    busy: bool,
    failed_phase: str,
) -> None:
    hooks = Hooks([_snapshot(busy=busy)])
    setattr(hooks, hook_name, _raise_unexpected_hook_error)

    result = _coordinator().run(
        "alas",
        operation_id="handover-hook-error",
        session_id="session-1",
        hooks=hooks,
    )

    assert result.ok is False
    assert result.code == "RUNTIME_HANDOVER_HOOK_FAILED"
    assert result.details["failed_phase"] == failed_phase
    assert result.details["hook_error"] == "RuntimeError"
    assert result.phases[-1] == "failed"


def test_handover_preserves_typed_application_hook_cause() -> None:
    hooks = Hooks([_snapshot(busy=False)])

    def fail_return_to_main(*_args: object, **_kwargs: object) -> object:
        error = OperationFailedError("Не удалось подтвердить главный экран")
        error.handover_step = "main_check_before_navigation"
        error.cause_type = "PostconditionFailedError"
        error.cause_code = "postcondition_failed"
        error.cause_message = "UI не подтвердил главный экран"
        raise error

    hooks.return_to_main = fail_return_to_main  # type: ignore[method-assign]

    result = _coordinator().run(
        "alas",
        operation_id="handover-typed-hook-error",
        session_id="session-1",
        hooks=hooks,
    )

    assert result.ok is False
    assert result.code == "RUNTIME_HANDOVER_OPERATION_FAILED"
    assert result.message == "Не удалось подтвердить главный экран"
    assert result.details["failed_phase"] == "returning_to_main"
    assert result.details["handover_step"] == "main_check_before_navigation"
    assert result.details["cause_type"] == "PostconditionFailedError"
    assert result.details["cause_code"] == "postcondition_failed"
    assert result.details["cause_message"] == "UI не подтвердил главный экран"
    assert result.details["cause"] == {
        "type": "OperationFailedError",
        "code": "operation_failed",
        "message": "Не удалось подтвердить главный экран",
    }


def test_handover_converts_unexpected_begin_hook_error_to_fail_closed_result() -> None:
    hooks = Hooks([_snapshot(busy=False)])
    hooks.begin_handover = _raise_unexpected_hook_error  # type: ignore[method-assign]

    result = _coordinator().run(
        "alas",
        operation_id="handover-begin-error",
        session_id="session-1",
        hooks=hooks,
    )

    assert result.ok is False
    assert result.code == "RUNTIME_HANDOVER_HOOK_FAILED"
    assert result.details["failed_phase"] == "begin_handover"
    assert result.phases[-1] == "failed"


def test_handover_records_failed_phase_when_phase_hook_raises() -> None:
    hooks = Hooks([_snapshot(busy=False)])
    original_mark = hooks.mark_phase

    def fail_non_terminal_phase(
        profile: str,
        phase: RuntimePhase,
        operation_id: str,
        session_id: str | None,
    ) -> None:
        if phase is not RuntimePhase.FAILED:
            raise RuntimeError("синтетическая ошибка записи фазы")
        original_mark(profile, phase, operation_id, session_id)

    hooks.mark_phase = fail_non_terminal_phase  # type: ignore[method-assign]

    result = _coordinator().run(
        "alas",
        operation_id="handover-phase-error",
        session_id="session-1",
        hooks=hooks,
    )

    assert result.ok is False
    assert result.code == "RUNTIME_HANDOVER_HOOK_FAILED"
    assert result.details["failed_phase"] == "quiesce_requested"
    assert result.phases[-1] == "failed"
