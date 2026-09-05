from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread

import pytest

from module.application.runtime_state import (
    RuntimePhase,
    RuntimeStateError,
    RuntimeStateStore,
)


def _store(root: Path, timestamp: str = "2026-09-04T00:00:00+00:00") -> RuntimeStateStore:
    return RuntimeStateStore(root, now=lambda: timestamp)


def test_runtime_state_records_busy_handover_and_clears_stale_worker_identity(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    started = store.mark_worker_started(
        "alas",
        worker_pid=1001,
        worker_created_at=2001.0,
        operation_id="start-1",
    )
    assert started.phase is RuntimePhase.USER_PROFILE_IDLE
    assert started.worker_running is True

    busy = store.mark_task_started(
        "alas",
        "DailyTask",
        operation_id="task-1",
    )
    assert busy.phase is RuntimePhase.USER_PROFILE_BUSY
    assert busy.busy is True
    assert busy.current_task == "DailyTask"

    store.request_handover("alas", operation_id="handover-1", session_id="session-1")
    finished_during_handover = store.mark_task_finished(
        "alas",
        operation_id="task-1",
        session_id="session-1",
    )
    assert finished_during_handover.handover_requested is True
    store.mark_grace_period("alas", operation_id="handover-1", session_id="session-1")
    store.request_quiesce("alas", operation_id="handover-1", session_id="session-1")
    store.mark_draining("alas", operation_id="handover-1", session_id="session-1")
    stopped = store.mark_current_task_stopped(
        "alas",
        operation_id="handover-1",
        session_id="session-1",
    )

    assert stopped.phase is RuntimePhase.CURRENT_TASK_STOPPED
    assert stopped.worker_running is False
    assert stopped.busy is False
    assert stopped.worker_pid is None
    assert stopped.worker_created_at is None
    assert stopped.handover_requested is True


def test_runtime_state_rejects_mismatched_profile_keys_and_reports_stale_state(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, "2020-01-01T00:00:00+00:00")
    store.mark_worker_started("ap", worker_pid=1002, worker_created_at=2002.0)
    assert store.read("ap").freshness == "stale"

    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["profiles"]["ap"]["profile"] = "other"
    store.path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeStateError) as error:
        store.read_all()
    assert error.value.code == "RUNTIME_STATE_CORRUPT"


def test_runtime_state_rejects_far_future_timestamp_as_stale(tmp_path: Path) -> None:
    store = _store(tmp_path, datetime.now(UTC).isoformat())
    store.mark_worker_started("ap", worker_pid=1005, worker_created_at=2005.0)
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["profiles"]["ap"]["updated_at"] = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    store.path.write_text(json.dumps(payload), encoding="utf-8")

    assert store.read("ap").freshness == "stale"
    with pytest.raises(RuntimeStateError) as error:
        store.begin_handover("ap", operation_id="future-handover")
    assert error.value.code == "RUNTIME_HANDOVER_STATE_STALE"
    assert store.try_mark_task_started("ap", "DailyTask", operation_id="future-task") is False


def test_runtime_state_rejects_unsafe_profile_and_worker_timestamp(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(RuntimeStateError) as profile_error:
        store.mark_worker_started("../outside", worker_pid=1, worker_created_at=1.0)
    assert profile_error.value.code == "RUNTIME_PROFILE_INVALID"

    with pytest.raises(RuntimeStateError) as timestamp_error:
        store.mark_worker_started("ap", worker_pid=1, worker_created_at=float("nan"))
    assert timestamp_error.value.code == "RUNTIME_WORKER_ID_INVALID"


def test_runtime_state_does_not_claim_a_task_after_handover_request(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.mark_worker_started("alas", worker_pid=1003, worker_created_at=2003.0)
    store.request_handover("alas", operation_id="handover-2")

    assert store.try_mark_task_started("alas", "DailyTask", operation_id="task-2") is False
    snapshot = store.read("alas")
    assert snapshot.busy is False
    assert snapshot.handover_requested is True


def test_runtime_state_rejects_stale_worker_cleanup_for_another_operation(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.mark_worker_started(
        "alas",
        worker_pid=1006,
        worker_created_at=2006.0,
        operation_id="start-1",
        session_id="session-1",
    )

    with pytest.raises(RuntimeStateError) as error:
        store.mark_worker_stopped(
            "alas",
            expected_worker_pid=1006,
            expected_worker_created_at=2006.0,
            operation_id="start-2",
            session_id="session-2",
        )

    assert error.value.code == "RUNTIME_STATE_STALE_WRITE"
    snapshot = store.read("alas")
    assert snapshot.worker_running is True
    assert snapshot.operation_id == "start-1"
    assert snapshot.session_id == "session-1"


def test_runtime_state_begin_handover_atomically_blocks_concurrent_task_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, datetime.now(UTC).isoformat())
    store.mark_worker_started("alas", worker_pid=1004, worker_created_at=2004.0)

    begin_read = Event()
    allow_begin = Event()
    task_attempted = Event()
    outcomes: dict[str, object] = {}
    errors: list[BaseException] = []
    original_read = store._read_payload

    def paused_read() -> dict[str, object]:
        payload = original_read()
        if not begin_read.is_set():
            begin_read.set()
            if not allow_begin.wait(5):
                raise AssertionError("begin_handover не получил разрешение продолжить")
        return payload

    monkeypatch.setattr(store, "_read_payload", paused_read)

    def begin() -> None:
        try:
            outcomes["handover"] = store.begin_handover(
                "alas",
                operation_id="handover-atomic",
                session_id="session-atomic",
            )
        except Exception as exc:  # noqa: BLE001 - сохранить исключение из фонового тестового потока.
            errors.append(exc)

    def start_task() -> None:
        task_attempted.set()
        try:
            outcomes["task_started"] = store.try_mark_task_started(
                "alas",
                "DailyTask",
                operation_id="task-race",
            )
        except Exception as exc:  # noqa: BLE001 - сохранить исключение из фонового тестового потока.
            errors.append(exc)

    begin_thread = Thread(target=begin)
    task_thread = Thread(target=start_task)
    begin_thread.start()
    try:
        assert begin_read.wait(5)
        task_thread.start()
        assert task_attempted.wait(5)
    finally:
        allow_begin.set()
        begin_thread.join(timeout=5)
        task_thread.join(timeout=5)

    assert not begin_thread.is_alive()
    assert not task_thread.is_alive()
    assert errors == []
    handover = outcomes["handover"]
    assert handover is not None
    assert handover.phase is RuntimePhase.HANDOVER_REQUESTED
    assert handover.handover_requested is True
    assert outcomes["task_started"] is False
    assert store.read("alas").current_task is None
