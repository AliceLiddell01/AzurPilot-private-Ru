from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from module.application.runtime_control import (
    RuntimeControlOperation,
    RuntimeControlResult,
    RuntimeOwnerIdentity,
)
from module.application.runtime_state import RuntimeStateStore
from module.dev_runtime import (
    DevEnvironment,
    DevRuntimeMode,
    DevSessionManager,
    DevSessionState,
    DevTarget,
    ProcessIdentity,
)
from module.dev_runtime.bot_runtime import BotRuntimeFacade


class OwnerProcessBackend:
    def __init__(self, identity: ProcessIdentity) -> None:
        self.identity = identity

    def capture(self, pid: int) -> ProcessIdentity | None:
        return self.identity if pid == self.identity.pid else None


class BotRuntimeLifecycle:
    def __init__(self, root: Path) -> None:
        self.active = False
        self.session_id: str | None = None
        self.owner = RuntimeOwnerIdentity(pid=7001, created_at=8001.0)

    def start_profile(self, *, session_id: str, idempotency_key: str | None = None) -> RuntimeControlResult:
        self.active = True
        self.session_id = session_id
        return RuntimeControlResult(
            True,
            "RUNTIME_STARTED",
            "Профиль запущен",
            RuntimeControlOperation.START_PROFILE,
            "ap",
            session_id,
            idempotency_key or session_id,
            owner=self.owner,
        )

    def stop_profile(self, *, session_id: str, idempotency_key: str | None = None) -> RuntimeControlResult:
        if session_id != self.session_id:
            return RuntimeControlResult(
                False,
                "RUNTIME_OWNERSHIP_MISMATCH",
                "Сессия не владеет профилем",
                RuntimeControlOperation.STOP_PROFILE,
                "ap",
                session_id,
                idempotency_key or session_id,
                owner=self.owner,
            )
        self.active = False
        return RuntimeControlResult(
            True,
            "RUNTIME_STOPPED",
            "Профиль остановлен",
            RuntimeControlOperation.STOP_PROFILE,
            "ap",
            session_id,
            idempotency_key or session_id,
            owner=self.owner,
        )

    def owner_identity(self) -> RuntimeOwnerIdentity:
        return self.owner

    def matches_session(self, session_id: str, profile: str = "ap") -> bool:
        return self.active and profile == "ap" and session_id == self.session_id

    def worker_present(self, profile: str = "ap") -> bool:
        return self.active and profile == "ap"

    def ready(self, profile: str = "ap", session_id: str | None = None) -> tuple[bool, str]:
        return bool(
            self.active and profile == "ap" and (session_id is None or session_id == self.session_id)
        ), "Bot Runtime ready"


def _manager(tmp_path: Path) -> tuple[DevSessionManager, BotRuntimeLifecycle]:
    root = tmp_path.resolve()
    (root / "module").mkdir(parents=True)
    (root / "module" / "bot_runtime.py").write_text(
        "# синтетическая точка входа Bot Runtime\n",
        encoding="utf-8",
    )
    environment = DevEnvironment(
        repository_root=root,
        python_executable=root / ".venv" / "Scripts" / "python.exe",
        dev_target=DevTarget("ap"),
    )
    runtime = BotRuntimeLifecycle(root)
    identity = ProcessIdentity(
        pid=runtime.owner.pid,
        created_at=runtime.owner.created_at,
        executable=str(environment.python_executable),
        command_line=("module.bot_runtime",),
        cwd=str(root),
    )
    manager = DevSessionManager(
        environment,
        process_backend=OwnerProcessBackend(identity),
        bot_runtime=True,
        bot_runtime_lifecycle=runtime,
        storage_probe=lambda _environment: (True, "storage ready"),
        port_probe=lambda _host, _port: True,
        session_id_factory=lambda: "bot-runtime-session",
        now=lambda: datetime(2026, 9, 4, tzinfo=UTC),
    )
    manager._project_python_is_supported = lambda: True
    manager._profile_check = lambda: (True, "profile ready")
    return manager, runtime


def test_bot_runtime_stop_does_not_create_local_application_log_copy(tmp_path: Path) -> None:
    manager, runtime = _manager(tmp_path)

    started = manager.start()
    assert started.ok is True
    assert started.state == "running_owned"
    assert started.details["runtime_mode"] == "bot_runtime"
    assert manager.status().ok is True
    assert manager.status().details["runtime_mode"] == "bot_runtime"
    assert runtime.active is True

    stopped = manager.stop()
    assert stopped.ok is True
    assert stopped.state == "stopped"
    assert runtime.active is False
    assert not list(tmp_path.resolve().rglob("*.log"))


def test_bot_runtime_status_distinguishes_missing_lifecycle_matcher(tmp_path: Path) -> None:
    manager, runtime = _manager(tmp_path)
    assert manager.start().ok is True

    runtime.matches_session = None  # type: ignore[method-assign]

    status = manager.status()

    assert status.ok is False
    assert status.code == "DEV_RUNTIME_MODE_MISMATCH"
    assert "Bot Runtime manager" in status.message


def test_bot_runtime_rejects_standalone_process_session_marker(tmp_path: Path) -> None:
    manager, _runtime = _manager(tmp_path)
    assert manager.start().ok is True
    session = manager._read_session()
    assert session is not None
    manager._write_session(
        replace(session, runtime_mode=DevRuntimeMode.STANDALONE_PROCESS)
    )

    status = manager.status()

    assert status.ok is False
    assert status.code == "DEV_RUNTIME_MODE_MISMATCH"
    assert "Менеджер Dev Runtime" in status.message
    assert "DevSession" in status.message
    assert "standalone_process" in status.message


def test_bot_runtime_failure_preserves_worker_identity_when_stop_is_unconfirmed(
    tmp_path: Path,
) -> None:
    manager, runtime = _manager(tmp_path)

    def lost_ownership(_session_id: str, _profile: str = "ap") -> bool:
        return False

    def failed_stop(
        *, session_id: str, idempotency_key: str | None = None
    ) -> RuntimeControlResult:
        return RuntimeControlResult(
            False,
            "RUNTIME_STOP_UNCONFIRMED",
            "Профиль не подтвердил остановку",
            RuntimeControlOperation.STOP_PROFILE,
            "ap",
            session_id,
            idempotency_key or session_id,
            owner=runtime.owner,
        )

    runtime.matches_session = lost_ownership  # type: ignore[method-assign]
    runtime.stop_profile = failed_stop  # type: ignore[method-assign]

    failed = manager.start()

    assert failed.ok is False
    assert failed.code == "DEV_CLEANUP_FAILED"
    assert runtime.active is True
    persisted = manager._read_session()
    assert persisted is not None
    assert persisted.process is not None
    assert persisted.last_code == "DEV_CLEANUP_FAILED"


def test_bot_runtime_failure_preserves_handover_details(tmp_path: Path) -> None:
    manager, runtime = _manager(tmp_path)

    def failed_start(
        *, session_id: str, idempotency_key: str | None = None
    ) -> RuntimeControlResult:
        runtime.active = True
        runtime.session_id = session_id
        return RuntimeControlResult(
            False,
            "RUNTIME_HANDOVER_OPERATION_FAILED",
            "Handover не подтверждён",
            RuntimeControlOperation.START_PROFILE,
            "ap",
            session_id,
            idempotency_key or session_id,
            owner=runtime.owner,
            details={
                "handover": {
                    "ok": False,
                    "code": "RUNTIME_HANDOVER_OPERATION_FAILED",
                    "operation_id": "handover-1",
                    "phases": ["returning_to_main", "failed"],
                    "details": {
                        "failed_phase": "returning_to_main",
                        "handover_step": "device",
                        "cause_type": "ImportError",
                        "cause_message": "synthetic import failure",
                    },
                }
            },
        )

    runtime.start_profile = failed_start  # type: ignore[method-assign]

    failed = manager.start()

    assert failed.ok is False
    assert failed.code == "RUNTIME_HANDOVER_OPERATION_FAILED"
    assert failed.details["handover"] == {
        "ok": False,
        "code": "RUNTIME_HANDOVER_OPERATION_FAILED",
        "operation_id": "handover-1",
        "phases": ["returning_to_main", "failed"],
        "details": {
            "failed_phase": "returning_to_main",
            "handover_step": "device",
            "cause_type": "ImportError",
            "cause_message": "synthetic import failure",
        },
    }


def test_task_cleanup_preserves_bot_runtime_failure_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _runtime = _manager(tmp_path)
    assert manager.start().ok is True
    session = manager._read_session()
    assert session is not None
    cleanup = SimpleNamespace(ok=True, as_dict=lambda: {"confirmed": True})
    monkeypatch.setattr(
        manager,
        "_finalize_evidence_before_cleanup",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        manager,
        "_cleanup_task_state_locked",
        lambda **_kwargs: cleanup,
    )

    failed = manager._bot_runtime_start_failure(
        session,
        SimpleNamespace(catalog=object()),
        process_started=True,
        worker_stopped=True,
        code="DEV_BOT_RUNTIME_START_FAILED",
        message="Bot Runtime не подтвердил запуск",
        details={"handover": {"code": "RUNTIME_HANDOVER_FAILED"}},
    )

    assert failed.details == {
        "handover": {"code": "RUNTIME_HANDOVER_FAILED"},
        "task_cleanup": {"confirmed": True},
    }


def test_bot_runtime_manager_uses_new_stop_idempotency_key_for_each_attempt(
    tmp_path: Path,
) -> None:
    manager, runtime = _manager(tmp_path)
    started = manager.start()
    assert started.ok is True
    session = manager._read_session()
    assert session is not None
    keys: list[str | None] = []
    original_stop = runtime.stop_profile

    def record_stop(
        *, session_id: str, idempotency_key: str | None = None
    ) -> RuntimeControlResult:
        keys.append(idempotency_key)
        return original_stop(session_id=session_id, idempotency_key=idempotency_key)

    runtime.stop_profile = record_stop  # type: ignore[method-assign]

    assert manager._stop_bot_runtime_worker(session) is True
    assert manager._stop_bot_runtime_worker(session) is True
    assert len(keys) == 2
    assert keys[0] is not None
    assert keys[0] != keys[1]


def test_bot_runtime_runs_pre_execution_hook_before_owner_start(
    tmp_path: Path,
) -> None:
    manager, runtime = _manager(tmp_path)
    events: list[str] = []
    original_start = runtime.start_profile

    def record_start(
        *, session_id: str, idempotency_key: str | None = None
    ) -> RuntimeControlResult:
        events.append("start")
        return original_start(session_id=session_id, idempotency_key=idempotency_key)

    runtime.start_profile = record_start  # type: ignore[method-assign]

    started = manager.start_with_pre_execution_hook(
        before_process_launch=lambda _session_id: events.append("hook"),
    )

    assert started.ok is True
    assert events == ["hook", "start"]
    assert runtime.active is True
    assert manager.stop().ok is True


def test_bot_runtime_requires_worker_registry_identity_to_match_runtime_state(
    tmp_path: Path,
) -> None:
    runtime = BotRuntimeFacade(tmp_path)
    owner = RuntimeOwnerIdentity(pid=7001, created_at=8001.0)
    record = {"pid": 7010, "created_at": 8010.0}
    runtime._owner_reader = lambda: owner  # type: ignore[method-assign]
    runtime._owner_matches = lambda _owner: True  # type: ignore[method-assign]
    runtime._worker_record = lambda _profile: record  # type: ignore[method-assign]
    runtime._process_matches = lambda _record: True  # type: ignore[method-assign]
    RuntimeStateStore(tmp_path).mark_resource_ready(
        "ap",
        worker_pid=7010,
        worker_created_at=8010.0,
        operation_id="operation-1",
        session_id="session-1",
    )

    assert runtime.matches_session("session-1", "ap") is True
    record["created_at"] = 8011.0
    assert runtime.matches_session("session-1", "ap") is False


def test_bot_runtime_fails_closed_when_worker_registry_is_unknown(
    tmp_path: Path,
) -> None:
    runtime = BotRuntimeFacade(tmp_path)
    owner = RuntimeOwnerIdentity(pid=7001, created_at=8001.0)
    runtime._owner_reader = lambda: owner  # type: ignore[method-assign]
    runtime._owner_matches = lambda _owner: True  # type: ignore[method-assign]

    def unknown(_profile: str) -> dict | None:
        raise RuntimeError("registry unavailable")

    runtime._worker_record = unknown  # type: ignore[method-assign]

    assert runtime.worker_present("ap") is None
    assert runtime.ready("ap")[0] is False
    assert runtime.matches_session("session-1", "ap") is False


def test_bot_runtime_confirms_dead_snapshot_worker_after_registry_unregister(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = BotRuntimeFacade(tmp_path)
    runtime._worker_record = lambda _profile: None  # type: ignore[method-assign]
    RuntimeStateStore(tmp_path).mark_resource_ready(
        "ap",
        worker_pid=7010,
        worker_created_at=8010.0,
        operation_id="operation-1",
        session_id="session-1",
    )

    from module.dev_runtime import bot_runtime

    monkeypatch.setattr(bot_runtime, "process_matches", lambda _record: None)
    assert runtime.worker_present("ap") is False


def test_bot_runtime_keeps_live_snapshot_worker_present_after_registry_unregister(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = BotRuntimeFacade(tmp_path)
    runtime._worker_record = lambda _profile: None  # type: ignore[method-assign]
    RuntimeStateStore(tmp_path).mark_resource_ready(
        "ap",
        worker_pid=7010,
        worker_created_at=8010.0,
        operation_id="operation-1",
        session_id="session-1",
    )

    from module.dev_runtime import bot_runtime

    monkeypatch.setattr(bot_runtime, "process_matches", lambda _record: True)
    assert runtime.worker_present("ap") is True


def test_bot_runtime_fails_closed_on_unexpected_snapshot_identity_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = BotRuntimeFacade(tmp_path)
    runtime._worker_record = lambda _profile: None  # type: ignore[method-assign]
    RuntimeStateStore(tmp_path).mark_resource_ready(
        "ap",
        worker_pid=7010,
        worker_created_at=8010.0,
        operation_id="operation-1",
        session_id="session-1",
    )

    from module.dev_runtime import bot_runtime

    def unexpected_identity_error(_record: dict) -> bool:
        raise LookupError("синтетическая ошибка проверки identity")

    monkeypatch.setattr(bot_runtime, "process_matches", unexpected_identity_error)
    assert runtime.worker_present("ap") is None


def test_bot_runtime_recovery_does_not_close_marker_while_worker_is_present(tmp_path: Path) -> None:
    manager, runtime = _manager(tmp_path)
    started = manager.start()
    assert started.ok is True

    session = manager._read_session()
    assert session is not None
    session.process = None
    session.state = DevSessionState.FAILED
    manager._write_session(session)

    recovered = manager.recover()

    assert recovered.ok is False
    assert recovered.code == "DEV_OWNERSHIP_MISMATCH"
    assert runtime.active is True
    preserved = manager._read_session()
    assert preserved is not None
    assert preserved.state is DevSessionState.FAILED
    assert preserved.process is None


def test_bot_runtime_recovery_closes_marker_after_worker_registry_unregister(
    tmp_path: Path,
) -> None:
    manager, runtime = _manager(tmp_path)
    started = manager.start()
    assert started.ok is True

    session = manager._read_session()
    assert session is not None
    session.process = None
    session.state = DevSessionState.FAILED
    manager._write_session(session)
    runtime.active = False

    recovered = manager.recover()

    assert recovered.ok is True
    assert recovered.code == "DEV_STALE_RECOVERED"
    persisted = manager._read_session()
    assert persisted is not None
    assert persisted.state is DevSessionState.STOPPED
    assert persisted.process is None
