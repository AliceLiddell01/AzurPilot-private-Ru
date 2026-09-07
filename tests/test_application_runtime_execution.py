from __future__ import annotations

import pytest

from module.application.game_models import CurrentTaskState
from module.application.runtime_execution import (
    RuntimeExecutionReader,
    WorkerIdentityEvidence,
    WorkerIdentityStatus,
)
from module.application.runtime_state import RuntimePhase, RuntimeStateSnapshot


def _snapshot(
    *,
    worker_running: bool,
    busy: bool,
    current_task: str | None,
    freshness: str = "fresh",
    worker_pid: int | None = 1001,
    worker_created_at: float | None = 2001.0,
) -> RuntimeStateSnapshot:
    return RuntimeStateSnapshot(
        profile="ap",
        phase=(
            RuntimePhase.USER_PROFILE_BUSY
            if busy
            else RuntimePhase.USER_PROFILE_IDLE
            if worker_running
            else RuntimePhase.STOPPED
        ),
        worker_running=worker_running,
        busy=busy,
        current_task=current_task,
        operation_id=None,
        session_id=None,
        handover_requested=False,
        draining=False,
        stop_requested=False,
        terminal_state=None,
        worker_pid=worker_pid if worker_running else None,
        worker_created_at=worker_created_at if worker_running else None,
        updated_at="2026-09-08T00:00:00+00:00",
        freshness=freshness,
        provenance="hooks",
    )


class _StateReader:
    def __init__(self, snapshot: RuntimeStateSnapshot | None) -> None:
        self.snapshot = snapshot

    def read(self, profile: str) -> RuntimeStateSnapshot | None:
        assert profile == "ap"
        return self.snapshot


class _IdentityReader:
    def __init__(self, evidence: WorkerIdentityEvidence) -> None:
        self.evidence = evidence

    def read_worker_identity(self, profile: str) -> WorkerIdentityEvidence:
        assert profile == "ap"
        return self.evidence


def _reader(
    snapshot: RuntimeStateSnapshot | None,
    evidence: WorkerIdentityEvidence,
) -> RuntimeExecutionReader:
    return RuntimeExecutionReader(_StateReader(snapshot), _IdentityReader(evidence))


def test_runtime_execution_normalizes_running_with_exact_identity() -> None:
    result = _reader(
        _snapshot(worker_running=True, busy=True, current_task="Main"),
        WorkerIdentityEvidence(WorkerIdentityStatus.VERIFIED, 1001, 2001.0),
    ).read_current_task("ap")

    assert result.state is CurrentTaskState.RUNNING
    assert result.task == "Main"


def test_runtime_execution_normalizes_idle_without_inventing_a_task() -> None:
    result = _reader(
        _snapshot(worker_running=True, busy=False, current_task=None),
        WorkerIdentityEvidence(WorkerIdentityStatus.VERIFIED, 1001, 2001.0),
    ).read_current_task("ap")

    assert result.state is CurrentTaskState.IDLE
    assert result.task is None


def test_runtime_execution_normalizes_stopped_only_when_worker_is_absent() -> None:
    result = _reader(
        None,
        WorkerIdentityEvidence(WorkerIdentityStatus.ABSENT),
    ).read_current_task("ap")

    assert result.state is CurrentTaskState.STOPPED
    assert result.task is None


@pytest.mark.parametrize(
    ("snapshot", "evidence"),
    (
        (
            _snapshot(
                worker_running=True,
                busy=True,
                current_task="Main",
                freshness="stale",
            ),
            WorkerIdentityEvidence(WorkerIdentityStatus.VERIFIED, 1001, 2001.0),
        ),
        (
            _snapshot(worker_running=True, busy=True, current_task="Main"),
            WorkerIdentityEvidence(WorkerIdentityStatus.VERIFIED, 1002, 2001.0),
        ),
        (
            _snapshot(worker_running=True, busy=False, current_task=None),
            WorkerIdentityEvidence(WorkerIdentityStatus.UNKNOWN),
        ),
        (
            _snapshot(worker_running=False, busy=False, current_task=None),
            WorkerIdentityEvidence(WorkerIdentityStatus.VERIFIED, 1001, 2001.0),
        ),
    ),
)
def test_runtime_execution_fails_closed_for_untrusted_state(
    snapshot: RuntimeStateSnapshot,
    evidence: WorkerIdentityEvidence,
) -> None:
    result = _reader(snapshot, evidence).read_current_task("ap")

    assert result.state is CurrentTaskState.UNKNOWN
    assert result.task is None


def test_runtime_execution_fails_closed_on_reader_exception() -> None:
    class BrokenState:
        def read(self, profile: str) -> RuntimeStateSnapshot | None:
            raise RuntimeError("corrupt")

    reader = RuntimeExecutionReader(
        BrokenState(),  # type: ignore[arg-type]
        _IdentityReader(WorkerIdentityEvidence(WorkerIdentityStatus.ABSENT)),
    )

    result = reader.read_current_task("ap")

    assert result.state is CurrentTaskState.UNKNOWN
    assert result.task is None


def test_runtime_execution_fails_closed_on_identity_reader_exception() -> None:
    class BrokenIdentity:
        def read_worker_identity(self, profile: str) -> WorkerIdentityEvidence:
            raise RuntimeError("identity недоступна")

    reader = RuntimeExecutionReader(
        _StateReader(_snapshot(worker_running=True, busy=True, current_task="Main")),
        BrokenIdentity(),  # type: ignore[arg-type]
    )

    result = reader.read_current_task("ap")

    assert result.state is CurrentTaskState.UNKNOWN
    assert result.task is None
