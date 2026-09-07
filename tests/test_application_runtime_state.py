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
    _scoped_path,
)
from module.application.scheduler_runtime import SchedulerRuntimeStateReader


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


def test_runtime_state_handover_keeps_source_worker_session_ownership(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, datetime.now(UTC).isoformat())
    store.mark_worker_started(
        "alas",
        worker_pid=1010,
        worker_created_at=2010.0,
        operation_id="user-start",
    )
    initial = store.begin_handover(
        "alas",
        operation_id="handover-ownership",
        session_id="dev-session",
    )
    assert initial is not None
    assert initial.session_id is None

    phase = store.mark_preemption_notice(
        "alas",
        operation_id="handover-ownership",
        session_id="dev-session",
    )
    assert phase.session_id is None

    quiesce = store.request_quiesce(
        "alas",
        operation_id="handover-ownership",
        session_id="dev-session",
    )
    assert quiesce.session_id is None

    failed = store.mark_failed(
        "alas",
        operation_id="handover-ownership",
        session_id="dev-session",
        terminal_state="handover_failed",
    )
    assert failed.session_id is None
    assert failed.worker_running is True
    assert failed.handover_requested is False


def test_runtime_state_reconciles_stale_user_session_ownership_from_worker_identity(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.mark_worker_started(
        "alas",
        worker_pid=1011,
        worker_created_at=2011.0,
        operation_id="stale-handover",
        session_id="stale-dev-session",
    )

    reconciled = store.reconcile_profile_ownership(
        {"alas": {"pid": 1011, "created_at": 2011.0}},
        session_owner_profile="ap",
    )

    assert reconciled == ("alas",)
    snapshot = store.read("alas")
    assert snapshot is not None
    assert snapshot.session_id is None
    assert snapshot.worker_pid == 1011
    assert snapshot.provenance == "runtime_reconciliation"


def test_runtime_state_does_not_reconcile_user_ownership_during_active_handover(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, datetime.now(UTC).isoformat())
    store.mark_worker_started(
        "alas",
        worker_pid=1012,
        worker_created_at=2012.0,
        operation_id="user-start",
        session_id="stale-dev-session",
    )
    store.request_handover(
        "alas",
        operation_id="handover-active",
        session_id="target-session",
    )

    with pytest.raises(RuntimeStateError) as error:
        store.reconcile_profile_ownership(
            {"alas": {"pid": 1012, "created_at": 2012.0}},
            session_owner_profile="ap",
        )

    assert error.value.code == "RUNTIME_STATE_RECONCILIATION_REQUIRED"


def test_runtime_state_reconciles_dead_orphan_worker_to_canonical_stopped(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.mark_worker_started(
        "alas",
        worker_pid=1013,
        worker_created_at=2013.0,
        operation_id="orphan-start",
        session_id="orphan-session",
    )

    reconciled = store.reconcile_profile_ownership(
        {},
        session_owner_profile="ap",
        worker_identity_checker=lambda _pid, _created_at: None,
    )

    assert reconciled == ("alas",)
    snapshot = store.read("alas")
    assert snapshot is not None
    assert snapshot.phase is RuntimePhase.STOPPED
    assert snapshot.worker_running is False
    assert snapshot.current_task is None
    assert snapshot.session_id is None
    assert snapshot.worker_pid is None


def test_runtime_state_keeps_live_orphan_fail_closed(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.mark_worker_started(
        "alas",
        worker_pid=1014,
        worker_created_at=2014.0,
        operation_id="orphan-start",
        session_id="orphan-session",
    )

    with pytest.raises(RuntimeStateError) as error:
        store.reconcile_profile_ownership(
            {},
            session_owner_profile="ap",
            worker_identity_checker=lambda _pid, _created_at: True,
        )

    assert error.value.code == "RUNTIME_STATE_RECONCILIATION_REQUIRED"


def test_runtime_state_heartbeat_refreshes_stale_worker_without_changing_task_state(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    timestamps = iter(
        [
            (now - timedelta(seconds=180)).isoformat(),
            now.isoformat(),
        ]
    )
    store = RuntimeStateStore(tmp_path, now=lambda: next(timestamps))
    store.mark_worker_started(
        "alas",
        worker_pid=1015,
        worker_created_at=2015.0,
        operation_id="worker-start",
    )

    stale = store.read("alas")
    assert stale is not None
    assert stale.freshness == "stale"

    refreshed = store.refresh_worker_heartbeat(
        "alas",
        worker_pid=1015,
        worker_created_at=2015.0,
        operation_id="worker-start",
    )

    assert refreshed is not None
    assert refreshed.freshness == "fresh"
    assert refreshed.phase is RuntimePhase.USER_PROFILE_IDLE
    assert refreshed.worker_running is True
    assert refreshed.busy is False
    assert refreshed.current_task is None
    assert refreshed.worker_pid == 1015
    assert refreshed.worker_created_at == 2015.0


def test_runtime_state_heartbeat_rejects_reused_worker_identity(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, datetime.now(UTC).isoformat())
    store.mark_worker_started(
        "alas",
        worker_pid=1016,
        worker_created_at=2016.0,
        operation_id="worker-start",
    )

    with pytest.raises(RuntimeStateError) as error:
        store.refresh_worker_heartbeat(
            "alas",
            worker_pid=2016,
            worker_created_at=3016.0,
            operation_id="worker-start",
        )

    assert error.value.code == "RUNTIME_STATE_STALE_WRITE"


def test_runtime_state_reconciles_old_persisted_schema_from_empty_worker_registry(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profiles": {
                    "alas": {
                        "schema_version": 1,
                        "phase": "stopped",
                        "worker_running": False,
                    },
                    "ap": {
                        "schema_version": 1,
                        "phase": "failed",
                        "worker_running": True,
                        "worker_pid": None,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeStateError) as error:
        store.read_all()
    assert error.value.code == "RUNTIME_STATE_SCHEMA_MISMATCH"

    assert store.reconcile_with_authoritative_workers({}) is True
    assert json.loads(store.path.read_text(encoding="utf-8")) == {
        "schema_version": 2,
        "profiles": {},
    }
    assert store.read_all() == {}

    recovered = store.mark_worker_started(
        "ap",
        worker_pid=1234,
        worker_created_at=5678.0,
        operation_id="upgrade-start",
    )
    assert recovered.worker_running is True
    assert recovered.phase is RuntimePhase.USER_PROFILE_IDLE


def test_runtime_state_does_not_discard_incompatible_state_with_authoritative_worker(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps({"schema_version": 1, "profiles": {"alas": {}}}),
        encoding="utf-8",
    )
    before = store.path.read_bytes()

    with pytest.raises(RuntimeStateError) as error:
        store.reconcile_with_authoritative_workers(
            {"alas": {"pid": 1234, "created_at": 5678.0}}
        )

    assert error.value.code == "RUNTIME_STATE_SCHEMA_MISMATCH"
    assert error.value.details == {
        "recovery": "blocked",
        "authoritative_worker_profiles": ["alas"],
        "cause_code": "RUNTIME_STATE_SCHEMA_MISMATCH",
    }
    assert store.path.read_bytes() == before


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


def test_runtime_state_rejects_parent_directory_in_scoped_path(tmp_path: Path) -> None:
    with pytest.raises(RuntimeStateError) as error:
        _scoped_path(tmp_path, "config/../outside")

    assert error.value.code == "RUNTIME_STATE_PATH_INVALID"


@pytest.mark.parametrize("stop_kind", ["current_task", "worker"])
def test_runtime_state_task_finish_does_not_resurrect_stopped_worker(
    tmp_path: Path,
    stop_kind: str,
) -> None:
    store = _store(tmp_path)
    store.mark_worker_started(
        "alas",
        worker_pid=1007,
        worker_created_at=2007.0,
        operation_id="runtime-1",
    )
    store.mark_task_started("alas", "DailyTask", operation_id="runtime-1")
    if stop_kind == "current_task":
        store.request_handover("alas", operation_id="handover-1")
        stopped = store.mark_current_task_stopped("alas", operation_id="handover-1")
    else:
        stopped = store.mark_worker_stopped(
            "alas",
            expected_worker_pid=1007,
            expected_worker_created_at=2007.0,
            operation_id="runtime-1",
        )

    finished = store.mark_task_finished("alas", operation_id="runtime-1")

    assert stopped.worker_running is False
    assert finished.worker_running is False
    assert finished.busy is False
    assert finished.current_task is None
    assert finished.worker_pid is None
    assert finished.worker_created_at is None


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

    # begin_handover удерживает ту же блокировку при чтении и записи; поэтому
    # try_mark_task_started должен дождаться завершения этой транзакции.

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


def test_scheduler_membership_and_next_run_do_not_close_active_execution(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    task = "SyntheticTask"
    store = RuntimeStateStore(tmp_path, now=lambda: now.isoformat())
    store.mark_worker_started(
        "alas",
        worker_pid=1101,
        worker_created_at=2101.0,
        operation_id="worker-start",
    )
    store.mark_task_started(
        "alas",
        task,
        operation_id="task-execution",
    )

    def assert_active_execution() -> None:
        snapshot = store.read("alas")
        assert snapshot is not None
        assert snapshot.phase is RuntimePhase.USER_PROFILE_BUSY
        assert snapshot.busy is True
        assert snapshot.current_task == task

    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    config_path = config / "alas.json"
    reader = SchedulerRuntimeStateReader(tmp_path)

    def write_scheduler(*, enabled: bool, next_run: datetime) -> None:
        config_path.write_text(
            json.dumps(
                {
                    task: {
                        "Scheduler": {
                            "Enable": enabled,
                            "NextRun": next_run.isoformat(),
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

    future = now + timedelta(hours=1)
    write_scheduler(enabled=True, next_run=future)
    waiting = reader.read_queue("alas", (task,))
    assert [entry.task for entry in waiting] == [task]
    assert waiting[0].next_run > now
    assert_active_execution()

    # Сброс NextRun делает следующую scheduler-вызов eligible, но не
    # создаёт второй execution того же worker.
    write_scheduler(enabled=True, next_run=now - timedelta(seconds=1))
    pending = reader.read_queue("alas", (task,))
    assert [entry.task for entry in pending] == [task]
    assert pending[0].next_run < now
    assert_active_execution()

    write_scheduler(enabled=False, next_run=future)
    assert reader.read_queue("alas", (task,)) == ()
    assert_active_execution()

    # Последующий task_delay снова меняет только scheduler domain.
    write_scheduler(enabled=True, next_run=future)
    delayed = reader.read_queue("alas", (task,))
    assert delayed[0].next_run > now
    still_active = store.read("alas")
    assert still_active is not None
    assert still_active.phase is RuntimePhase.USER_PROFILE_BUSY
    assert still_active.busy is True
    assert still_active.current_task == task

    finished = store.mark_task_finished(
        "alas",
        task,
        operation_id="task-execution",
    )
    assert finished.phase is RuntimePhase.USER_PROFILE_IDLE
    assert finished.busy is False
    assert finished.current_task is None


@pytest.mark.parametrize(
    "changes",
    (
        {
            "phase": RuntimePhase.USER_PROFILE_IDLE.value,
            "busy": True,
            "current_task": "SyntheticTask",
        },
        {
            "phase": RuntimePhase.RESOURCE_READY.value,
            "busy": True,
            "current_task": "SyntheticTask",
        },
        {
            "phase": RuntimePhase.USER_PROFILE_BUSY.value,
            "busy": False,
            "current_task": None,
        },
        {
            "phase": RuntimePhase.QUIESCE_REQUESTED.value,
            "handover_requested": True,
            "draining": False,
            "stop_requested": False,
        },
    ),
)
def test_runtime_state_rejects_inconsistent_phase_aggregate(
    tmp_path: Path,
    changes: dict[str, object],
) -> None:
    store = _store(tmp_path)
    store.mark_worker_started("alas", worker_pid=1102, worker_created_at=2102.0)
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["profiles"]["alas"].update(changes)
    store.path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeStateError) as error:
        store.read("alas")

    assert error.value.code == "RUNTIME_STATE_CORRUPT"


def test_runtime_state_rejects_duplicate_task_start_without_overwriting_task(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.mark_worker_started("alas", worker_pid=1103, worker_created_at=2103.0)
    store.mark_task_started("alas", "FirstTask", operation_id="first")

    with pytest.raises(RuntimeStateError) as error:
        store.mark_task_started("alas", "SecondTask", operation_id="second")
    assert error.value.code == "RUNTIME_STATE_TASK_ALREADY_ACTIVE"
    assert store.try_mark_task_started("alas", "SecondTask") is False

    current = store.read("alas")
    assert current is not None
    assert current.current_task == "FirstTask"
    assert current.busy is True


def test_runtime_state_preserves_handover_operation_when_source_finishes_task(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, datetime.now(UTC).isoformat())
    store.mark_worker_started(
        "alas",
        worker_pid=1104,
        worker_created_at=2104.0,
        operation_id="source-start",
    )
    store.mark_task_started("alas", "FirstTask", operation_id="task-execution")
    handover = store.begin_handover(
        "alas",
        operation_id="handover-operation",
        session_id="target-session",
    )
    assert handover is not None
    assert handover.session_id is None

    finished = store.mark_task_finished(
        "alas",
        "FirstTask",
        operation_id="task-execution",
    )

    assert finished.phase is RuntimePhase.HANDOVER_REQUESTED
    assert finished.handover_requested is True
    assert finished.operation_id == "handover-operation"
    assert finished.session_id is None
    assert finished.busy is False
    assert finished.current_task is None


def test_runtime_state_fences_late_finish_from_old_worker_boot(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.mark_worker_started(
        "alas",
        worker_pid=1105,
        worker_created_at=2105.0,
        operation_id="old-start",
    )
    store.mark_task_started("alas", "OldTask", operation_id="old-task")
    store.mark_worker_stopped(
        "alas",
        expected_worker_pid=1105,
        expected_worker_created_at=2105.0,
        operation_id="old-task",
    )
    store.mark_worker_started(
        "alas",
        worker_pid=1205,
        worker_created_at=2205.0,
        operation_id="new-start",
    )
    store.mark_task_started("alas", "NewTask", operation_id="new-task")

    with pytest.raises(RuntimeStateError) as error:
        store.mark_task_finished(
            "alas",
            "OldTask",
            expected_worker_pid=1105,
            expected_worker_created_at=2105.0,
            operation_id="old-task",
        )

    assert error.value.code == "RUNTIME_STATE_STALE_WRITE"
    current = store.read("alas")
    assert current is not None
    assert current.worker_pid == 1205
    assert current.worker_created_at == 2205.0
    assert current.current_task == "NewTask"
    assert current.busy is True


def test_runtime_state_reconciles_orphan_worker_without_scheduler_guessing(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.mark_worker_started(
        "alas",
        worker_pid=1106,
        worker_created_at=2106.0,
        operation_id="orphan-start",
    )
    store.mark_task_started("alas", "OrphanTask", operation_id="orphan-task")

    reconciled = store.reconcile_stale_workers(
        {},
        worker_identity_checker=lambda _pid, _created_at: None,
    )

    assert reconciled == ("alas",)
    snapshot = store.read("alas")
    assert snapshot is not None
    assert snapshot.phase is RuntimePhase.STOPPED
    assert snapshot.worker_running is False
    assert snapshot.busy is False
    assert snapshot.current_task is None


def test_runtime_state_does_not_reconcile_worker_when_identity_is_live(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.mark_worker_started("alas", worker_pid=1107, worker_created_at=2107.0)

    with pytest.raises(RuntimeStateError) as error:
        store.reconcile_stale_workers(
            {},
            worker_identity_checker=lambda _pid, _created_at: True,
        )

    assert error.value.code == "RUNTIME_STATE_RECONCILIATION_REQUIRED"
    snapshot = store.read("alas")
    assert snapshot is not None
    assert snapshot.worker_running is True
