from __future__ import annotations

import json
from collections import deque
from pathlib import Path

from module.application.runtime_handover import (
    HandoverPolicy,
    ProfileHandoverCoordinator,
)
from module.application.runtime_state import RuntimePhase, RuntimeStateSnapshot
from module.application.scheduler_runtime import SchedulerRuntimeStateReader


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


class Hooks:
    def __init__(self, states: list[RuntimeStateSnapshot | None]) -> None:
        self.states = deque(states)
        self.phases: list[str] = []
        self.notifications = 0
        self.quiesce_requests = 0
        self.wait_calls: list[float] = []
        self.return_calls = 0
        self.main_confirmed = True

    def read_state(self, profile: str) -> RuntimeStateSnapshot | None:
        if len(self.states) > 1:
            return self.states.popleft()
        return self.states[0] if self.states else None

    def mark_phase(self, profile: str, phase: RuntimePhase, operation_id: str, session_id: str | None) -> None:
        self.phases.append(phase.value)

    def notify_preemption(self, profile: str, operation_id: str, session_id: str | None) -> bool:
        self.notifications += 1
        return True

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
    assert hooks.notifications == 0
    assert hooks.quiesce_requests == 1
    assert hooks.return_calls == 1
    assert hooks.phases == [
        "handover_requested",
        "quiesce_requested",
        "current_task_draining",
        "current_task_stopped",
        "returning_to_main",
        "main_confirmed",
    ]


def test_busy_handover_warns_before_grace_and_does_not_use_hard_stop() -> None:
    hooks = Hooks([_snapshot(busy=True), _snapshot(busy=False)])
    result = _coordinator().run(
        "alas",
        operation_id="handover-2",
        session_id=None,
        hooks=hooks,
    )

    assert result.ok is True
    assert hooks.notifications == 1
    assert hooks.wait_calls == [3]
    assert hooks.phases[:3] == [
        "handover_requested",
        "preemption_notice",
        "grace_period",
    ]
    assert hooks.phases[-3:] == ["current_task_stopped", "returning_to_main", "main_confirmed"]


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


def test_handover_keeps_user_scheduler_semantic_fingerprint(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "alas.json").write_text(
        json.dumps(
            {
                "DailyTask": {
                    "Scheduler": {
                        "Enable": False,
                        "NextRun": "2026-09-05T01:00:00+00:00",
                    }
                },
                "WeeklyTask": {
                    "Scheduler": {
                        "Enable": True,
                        "NextRun": "2026-09-05T00:30:00+00:00",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    reader = SchedulerRuntimeStateReader(tmp_path)
    before = reader.semantic_fingerprint("alas", ("DailyTask", "WeeklyTask"))

    result = _coordinator().run(
        "alas",
        operation_id="handover-scheduler",
        session_id=None,
        hooks=Hooks([_snapshot(busy=False)]),
    )

    assert result.ok is True
    assert reader.semantic_fingerprint("alas", ("DailyTask", "WeeklyTask")) == before
