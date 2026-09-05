from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from module.dev_runtime import (
    ConfiguredRuntimeBackend,
    ControlAction,
    ControlOutcome,
    DevEnvironment,
    DevTarget,
    DevTargetRegistry,
    RuntimeControlManager,
    RuntimeSessionState,
    RuntimeSnapshot,
)
from module.dev_runtime import control as control_module

_TARGET_NAME = "synthetic-target"
_NOW = datetime(2026, 8, 31, tzinfo=UTC)


def _environment(tmp_path: Path) -> DevEnvironment:
    root = tmp_path.resolve()
    (root / "module").mkdir(parents=True)
    config = root / "config"
    config.mkdir()
    (config / f"{_TARGET_NAME}.json").write_text(
        json.dumps(
            {
                "Alas": {
                    "Emulator": {
                        "Serial": "127.0.0.1:5555",
                        "PackageName": "com.example.azurpilot",
                    }
                },
                "General": {},
                "SyntheticTask": {"Scheduler": {}},
            }
        ),
        encoding="utf-8",
    )
    (root / "gui.py").write_text("# тестовый gui\n", encoding="utf-8")
    return DevEnvironment(
        repository_root=root,
        python_executable=root / ".venv" / "Scripts" / "python.exe",
        dev_target=DevTarget(_TARGET_NAME),
    )


class _FakeRuntimeBackend:
    def __init__(
        self,
        *,
        emulator_running: bool | None = True,
        emulator_detected: bool | None = True,
        emulator_ready: bool | None = True,
        adb_reachable: bool | None = True,
        game_running: bool | None = False,
        game_foreground: bool | None = False,
        unrelated_adb_devices: bool | None = False,
    ) -> None:
        self.state = RuntimeSnapshot(
            target_configured=True,
            emulator_detected=emulator_detected,
            emulator_running=emulator_running,
            emulator_ready=emulator_ready,
            adb_reachable=adb_reachable,
            adb_state="device" if adb_reachable else "unavailable",
            game_reachable=adb_reachable,
            game_foreground=game_foreground,
            game_running=game_running,
            unrelated_adb_devices=unrelated_adb_devices,
        )
        self.calls: list[str] = []
        self.pending: str | None = None
        self.fail: str | None = None

    def snapshot(self) -> RuntimeSnapshot:
        if self.fail == "snapshot":
            raise RuntimeError("synthetic status failure")
        return self.state

    def start_emulator(self) -> object:
        self.calls.append("start_emulator")
        if self.fail == "start_emulator":
            raise RuntimeError("synthetic start failure")
        self.pending = "emulator_start"
        self.state = replace(
            self.state,
            emulator_detected=True,
            emulator_running=True,
            emulator_ready=False,
        )
        return True

    def stop_emulator(self) -> object:
        self.calls.append("stop_emulator")
        if self.fail == "stop_emulator":
            return False
        self.pending = None
        self.state = replace(
            self.state,
            emulator_detected=False,
            emulator_running=False,
            emulator_ready=False,
            adb_reachable=False,
            adb_state="unavailable",
            game_reachable=False,
            game_foreground=False,
            game_running=False,
        )
        return True

    def start_game(self) -> object:
        self.calls.append("start_game")
        if self.fail == "start_game":
            return False
        self.pending = "game_start"
        self.state = replace(self.state, game_running=True, game_foreground=False)
        return True

    def stop_game(self) -> object:
        self.calls.append("stop_game")
        if self.fail == "stop_game":
            raise RuntimeError("synthetic stop failure")
        self.pending = None
        self.state = replace(self.state, game_running=False, game_foreground=False)
        return True

    def restart_adb(self) -> object:
        self.calls.append("restart_adb")
        if self.fail == "restart_adb":
            return False
        self.pending = "adb_restart"
        self.state = replace(
            self.state,
            adb_reachable=False,
            adb_state="unavailable",
            game_reachable=False,
        )
        return True

    def settle(self) -> None:
        if self.pending == "emulator_start":
            self.state = replace(
                self.state,
                emulator_detected=True,
                emulator_running=True,
                emulator_ready=True,
                adb_reachable=True,
                adb_state="device",
                game_reachable=True,
            )
        elif self.pending == "game_start":
            self.state = replace(self.state, game_running=True, game_foreground=True)
        elif self.pending == "adb_restart":
            self.state = replace(
                self.state,
                adb_reachable=True,
                adb_state="device",
                game_reachable=True,
            )
        self.pending = None


@pytest.fixture
def supervisor_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(control_module, "_process_created_at", lambda _pid: 123.0)
    monkeypatch.setattr(control_module, "_process_matches", lambda _pid, _created_at: True)


def _manager(
    environment: DevEnvironment,
    backend: _FakeRuntimeBackend,
    *,
    session_state: RuntimeSessionState | str | None = None,
    smoke_active: bool = False,
    sleep=None,
    monotonic=None,
    action_timeout: float = 5.0,
) -> RuntimeControlManager:
    sleep_callback = sleep or (lambda _seconds: backend.settle())
    return RuntimeControlManager(
        environment,
        backend_factory=lambda _environment: backend,
        session_state_provider=lambda: session_state,
        smoke_active_provider=lambda: smoke_active,
        supervisor_launcher=lambda _environment, _control_id: SimpleNamespace(pid=os.getpid()),
        now=lambda: _NOW,
        sleep=sleep_callback,
        monotonic=monotonic,
        action_timeouts={action: action_timeout for action in ControlAction},
    )


def _accepted(manager: RuntimeControlManager, action: ControlAction) -> str:
    result = manager.start(action)
    assert result.ok is True
    operation = result.details["control_operation"]
    assert isinstance(operation, dict)
    control_id = operation["control_id"]
    assert isinstance(control_id, str)
    return control_id


def _mark_supervisor_claimed(manager: RuntimeControlManager, control_id: str) -> None:
    operation = manager.store.read()
    assert operation is not None
    assert operation.control_id == control_id
    manager.store.write(replace(operation, supervisor_pid=4242, supervisor_created_at=123.0))


def _finish(manager: RuntimeControlManager, control_id: str):
    result = manager.execute(control_id)
    assert result.details["control_operation"]["state"] == "FINISHED"
    return result


def test_runtime_status_is_read_only_and_reports_bounded_state(
    tmp_path: Path,
    supervisor_identity: None,
) -> None:
    environment = _environment(tmp_path)
    backend = _FakeRuntimeBackend()
    manager = _manager(environment, backend)

    result = manager.status()

    assert result.ok is True
    assert result.code == "DEV_RUNTIME_STATUS_READY"
    assert result.details["emulator"]["readiness"] is True
    assert result.details["game"]["running"] is False
    assert backend.calls == []
    assert not environment.control_root.exists()


def test_existing_control_operation_without_lock_file_is_read_under_lock(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    manager = _manager(environment, _FakeRuntimeBackend())
    control_id = _accepted(manager, ControlAction.START_GAME)
    manager.store.lock_path.unlink()

    with manager.store.lock(create=False):
        operation = manager.store.read()

    assert operation is not None
    assert operation.control_id == control_id
    assert manager.store.lock_path.exists()


@pytest.mark.parametrize(
    "provider_error",
    [
        ValueError("synthetic value error"),
        OSError("synthetic OS error"),
        TimeoutError("synthetic timeout"),
    ],
)
def test_runtime_status_fails_closed_for_non_control_provider_errors(
    tmp_path: Path,
    provider_error: Exception,
) -> None:
    environment = _environment(tmp_path)
    manager = _manager(environment, _FakeRuntimeBackend())

    def broken_session_state() -> RuntimeSessionState:
        raise provider_error

    manager.session_state_provider = broken_session_state

    result = manager.status()

    assert result.ok is False
    assert result.code == "DEV_CONTROL_PRECONDITION_UNKNOWN"
    assert result.details["dev_session"]["state"] is None
    assert result.details["smoke"]["active"] is None


def test_runtime_status_does_not_persist_orphan_reconciliation(
    tmp_path: Path,
    supervisor_identity: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment(tmp_path)
    manager = _manager(environment, _FakeRuntimeBackend())
    _mark_supervisor_claimed(manager, _accepted(manager, ControlAction.START_GAME))
    before = manager.store.operation_path.read_bytes()
    monkeypatch.setattr(control_module, "_process_matches", lambda _pid, _created_at: False)

    result = manager.status()

    assert result.ok is True
    assert result.details["control_operation"]["active"] is False
    assert result.details["control_operation"]["operation"]["outcome"] == ControlOutcome.ABORTED.value
    assert manager.store.operation_path.read_bytes() == before


def test_created_control_operation_survives_supervisor_launch_grace(
    tmp_path: Path,
    supervisor_identity: None,
) -> None:
    environment = _environment(tmp_path)
    manager = _manager(environment, _FakeRuntimeBackend())
    operation = manager._reserve_operation(ControlAction.START_GAME)

    during_launch = manager.get_operation(operation.control_id)

    assert during_launch.ok is True
    assert during_launch.details["control_operation"]["state"] == "CREATED"
    assert during_launch.details["control_operation"]["outcome"] is None

    manager.now = lambda: _NOW + timedelta(seconds=11)
    after_grace = manager.get_operation(operation.control_id)

    assert after_grace.ok is False
    assert after_grace.details["control_operation"]["outcome"] == ControlOutcome.ABORTED.value


def test_supervisor_launcher_pid_is_not_persisted_before_child_claim(
    tmp_path: Path,
    supervisor_identity: None,
) -> None:
    environment = _environment(tmp_path)
    backend = _FakeRuntimeBackend()
    manager = RuntimeControlManager(
        environment,
        backend_factory=lambda _environment: backend,
        session_state_provider=lambda: None,
        smoke_active_provider=lambda: False,
        supervisor_launcher=lambda _environment, _control_id: SimpleNamespace(pid=4242),
        now=lambda: _NOW,
        sleep=lambda _seconds: backend.settle(),
        action_timeouts={action: 5.0 for action in ControlAction},
    )

    accepted = manager.start(ControlAction.START_GAME)
    assert accepted.ok is True
    operation = manager.store.read()
    assert operation is not None
    assert operation.supervisor_pid is None
    assert operation.supervisor_created_at is None

    control_id = accepted.details["control_operation"]["control_id"]
    finished = manager.execute(control_id)
    assert finished.ok is True
    assert finished.details["control_operation"]["outcome"] == ControlOutcome.PASS.value


def test_new_control_request_reconciles_crashed_previous_supervisor(
    tmp_path: Path,
    supervisor_identity: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment(tmp_path)
    backend = _FakeRuntimeBackend()
    first_manager = _manager(environment, backend)
    _mark_supervisor_claimed(first_manager, _accepted(first_manager, ControlAction.START_GAME))

    monkeypatch.setattr(control_module, "_process_matches", lambda _pid, _created_at: False)
    second_manager = _manager(environment, backend)
    result = second_manager.start(ControlAction.STOP_GAME)

    assert result.ok is True
    assert result.code == "DEV_CONTROL_ACCEPTED"


def test_emulator_start_waits_for_ready_and_persists_completion(
    tmp_path: Path,
    supervisor_identity: None,
) -> None:
    environment = _environment(tmp_path)
    backend = _FakeRuntimeBackend(
        emulator_running=False,
        emulator_detected=False,
        emulator_ready=False,
        adb_reachable=False,
        game_running=False,
        game_foreground=False,
    )
    sleeps: list[float] = []
    manager = _manager(environment, backend, sleep=lambda seconds: (sleeps.append(seconds), backend.settle()))

    control_id = _accepted(manager, ControlAction.START_EMULATOR)
    result = _finish(manager, control_id)

    assert result.ok is True
    assert result.details["control_operation"]["outcome"] == ControlOutcome.PASS.value
    assert result.details["control_operation"]["transitions"][-1]["state"] == "FINISHED"
    assert backend.calls == ["start_emulator"]
    assert sleeps


def test_emulator_stop_and_restart_use_state_predicates(
    tmp_path: Path,
    supervisor_identity: None,
) -> None:
    environment = _environment(tmp_path)
    backend = _FakeRuntimeBackend()
    manager = _manager(environment, backend)

    stopped = _finish(manager, _accepted(manager, ControlAction.STOP_EMULATOR))
    assert stopped.ok is True
    assert stopped.details["control_operation"]["outcome"] == ControlOutcome.PASS.value

    backend.state = replace(
        backend.state,
        emulator_detected=True,
        emulator_running=True,
        emulator_ready=True,
        adb_reachable=True,
        adb_state="device",
        game_reachable=True,
    )
    restarted = _finish(manager, _accepted(manager, ControlAction.RESTART_EMULATOR))
    assert restarted.ok is True
    assert restarted.details["control_operation"]["outcome"] == ControlOutcome.PASS.value
    assert backend.calls == ["stop_emulator", "stop_emulator", "start_emulator"]


def test_game_start_stop_and_restart_require_emulator_readiness(
    tmp_path: Path,
    supervisor_identity: None,
) -> None:
    environment = _environment(tmp_path)
    backend = _FakeRuntimeBackend(game_running=False, game_foreground=False)
    manager = _manager(environment, backend)

    started = _finish(manager, _accepted(manager, ControlAction.START_GAME))
    assert started.ok is True

    stopped = _finish(manager, _accepted(manager, ControlAction.STOP_GAME))
    assert stopped.ok is True

    backend.state = replace(backend.state, game_running=True, game_foreground=True)
    restarted = _finish(manager, _accepted(manager, ControlAction.RESTART_GAME))
    assert restarted.ok is True
    assert backend.calls == ["start_game", "stop_game", "stop_game", "start_game"]


def test_adb_restart_waits_for_reachability_without_using_app_target(
    tmp_path: Path,
    supervisor_identity: None,
) -> None:
    environment = _environment(tmp_path)
    backend = _FakeRuntimeBackend()
    manager = _manager(environment, backend)

    result = _finish(manager, _accepted(manager, ControlAction.RESTART_ADB))

    assert result.ok is True
    assert result.details["control_operation"]["outcome"] == ControlOutcome.PASS.value
    assert backend.calls == ["restart_adb"]


def test_adb_restart_does_not_require_emulator_readiness(
    tmp_path: Path,
    supervisor_identity: None,
) -> None:
    environment = _environment(tmp_path)
    backend = _FakeRuntimeBackend(
        emulator_ready=False,
        adb_reachable=False,
    )
    manager = _manager(environment, backend)

    result = _finish(manager, _accepted(manager, ControlAction.RESTART_ADB))

    assert result.ok is True
    assert result.details["control_operation"]["outcome"] == ControlOutcome.PASS.value
    assert backend.calls == ["restart_adb"]


@pytest.mark.parametrize(
    ("action", "backend_kwargs", "expected_code"),
    [
        (
            ControlAction.START_EMULATOR,
            {"emulator_running": None, "emulator_detected": None, "emulator_ready": None},
            "DEV_CONTROL_EMULATOR_STATE_UNKNOWN",
        ),
        (
            ControlAction.RESTART_EMULATOR,
            {"emulator_running": False, "emulator_detected": False, "emulator_ready": False},
            "DEV_CONTROL_EMULATOR_NOT_RUNNING",
        ),
        (
            ControlAction.STOP_GAME,
            {"game_running": None, "game_foreground": None},
            "DEV_CONTROL_GAME_STATE_UNKNOWN",
        ),
    ],
)
def test_runtime_control_fails_closed_on_unknown_or_invalid_precondition(
    tmp_path: Path,
    supervisor_identity: None,
    action: ControlAction,
    backend_kwargs: dict[str, object],
    expected_code: str,
) -> None:
    environment = _environment(tmp_path)
    backend = _FakeRuntimeBackend(**backend_kwargs)
    manager = _manager(environment, backend)

    result = _finish(manager, _accepted(manager, action))

    assert result.ok is False
    assert result.details["control_operation"]["outcome"] == ControlOutcome.PRECONDITION_FAILED.value
    assert result.details["control_operation"]["transitions"][-1]["code"] == expected_code
    assert backend.calls == []


def test_runtime_control_timeout_is_not_reported_as_success(
    tmp_path: Path,
    supervisor_identity: None,
) -> None:
    environment = _environment(tmp_path)
    backend = _FakeRuntimeBackend(
        emulator_running=False,
        emulator_detected=False,
        emulator_ready=False,
        adb_reachable=False,
    )
    manager: RuntimeControlManager

    def expire(_seconds: float) -> None:
        clock[0] = 1.0

    clock = [0.0]
    manager = _manager(environment, backend, sleep=expire, monotonic=lambda: clock[0], action_timeout=0.1)
    control_id = _accepted(manager, ControlAction.START_EMULATOR)

    result = _finish(manager, control_id)

    assert result.ok is False
    assert result.details["control_operation"]["outcome"] == ControlOutcome.TIMEOUT.value
    assert result.details["control_operation"]["transitions"][-1]["code"] == "DEV_CONTROL_TIMEOUT"
    assert any(
        item["state"] == "WAITING_READY"
        for item in result.details["control_operation"]["transitions"]
    )


def test_runtime_control_sanitizes_backend_failure_and_keeps_operation_record(
    tmp_path: Path,
    supervisor_identity: None,
) -> None:
    environment = _environment(tmp_path)
    backend = _FakeRuntimeBackend()
    backend.fail = "snapshot"
    manager = _manager(environment, backend)

    result = _finish(manager, _accepted(manager, ControlAction.START_GAME))

    assert result.ok is False
    assert result.code == "DEV_CONTROL_UNEXPECTED_FAILURE"
    assert result.details["control_operation"]["outcome"] == ControlOutcome.CONTROL_FAILED.value
    assert "synthetic" not in json.dumps(result.as_dict(), ensure_ascii=False)


def test_runtime_control_reports_backend_false_failure(
    tmp_path: Path,
    supervisor_identity: None,
) -> None:
    environment = _environment(tmp_path)
    backend = _FakeRuntimeBackend()
    backend.fail = "start_game"
    manager = _manager(environment, backend)

    result = _finish(manager, _accepted(manager, ControlAction.START_GAME))

    assert result.ok is False
    assert result.code == "DEV_CONTROL_GAME_START_FAILED"
    assert result.details["control_operation"]["outcome"] == ControlOutcome.CONTROL_FAILED.value
    assert backend.calls == ["start_game"]


def test_runtime_control_conflicts_with_session_smoke_and_second_operation(
    tmp_path: Path,
    supervisor_identity: None,
) -> None:
    environment = _environment(tmp_path)
    backend = _FakeRuntimeBackend()

    session_manager = _manager(
        environment,
        backend,
        session_state=RuntimeSessionState("running"),
    )
    session_result = session_manager.start(ControlAction.START_GAME)
    assert session_result.code == "DEV_CONTROL_CONFLICT_DEV_SESSION"
    assert session_result.details["outcome"] == ControlOutcome.CONFLICT.value

    smoke_manager = _manager(environment, backend, smoke_active=True)
    smoke_result = smoke_manager.start(ControlAction.START_GAME)
    assert smoke_result.code == "DEV_CONTROL_CONFLICT_SMOKE_ACTIVE"
    assert smoke_result.details["outcome"] == ControlOutcome.CONFLICT.value

    first_manager = _manager(environment, backend)
    _accepted(first_manager, ControlAction.START_GAME)
    second_manager = _manager(environment, backend)
    second_result = second_manager.start(ControlAction.STOP_GAME)
    assert second_result.code == "DEV_CONTROL_ACTIVE_CONFLICT"
    assert second_result.state == "conflict"
    assert second_result.details["outcome"] == ControlOutcome.CONFLICT.value


@pytest.mark.parametrize(
    "session_state",
    [
        RuntimeSessionState("stale"),
        RuntimeSessionState("failed", process_alive=True),
        RuntimeSessionState("stopped", process_alive=True),
    ],
)
def test_runtime_control_treats_stale_or_live_terminal_session_as_active(
    tmp_path: Path,
    supervisor_identity: None,
    session_state: RuntimeSessionState,
) -> None:
    environment = _environment(tmp_path)
    result = _manager(
        environment,
        _FakeRuntimeBackend(),
        session_state=session_state,
    ).start(ControlAction.START_GAME)

    assert result.ok is False
    assert result.code == "DEV_CONTROL_CONFLICT_DEV_SESSION"
    assert result.details["outcome"] == ControlOutcome.CONFLICT.value


def test_control_operation_survives_manager_reinstantiation_and_disconnect(
    tmp_path: Path,
    supervisor_identity: None,
) -> None:
    environment = _environment(tmp_path)
    backend = _FakeRuntimeBackend(game_running=False, game_foreground=False)
    first_manager = _manager(environment, backend)
    control_id = _accepted(first_manager, ControlAction.START_GAME)

    second_manager = _manager(environment, backend)
    result = _finish(second_manager, control_id)

    assert result.ok is True
    persisted = second_manager.get_operation(control_id)
    assert persisted.ok is True
    assert persisted.details["control_operation"]["outcome"] == ControlOutcome.PASS.value
    stored = second_manager.store.read()
    assert stored is not None
    assert stored.target_profile_name == _TARGET_NAME
    assert len(persisted.details["control_operation"]["target_identity"]) == 64
    assert len(persisted.details["control_operation"]["runtime_config_fingerprint"]) == 64
    assert "supervisor_pid" not in json.dumps(persisted.as_dict(), ensure_ascii=False)


def test_control_operation_fails_closed_after_registry_target_switch(
    tmp_path: Path,
    supervisor_identity: None,
) -> None:
    environment = _environment(tmp_path)
    target_b = "target-b"
    (environment.repository_root / "config" / f"{target_b}.json").write_text(
        json.dumps(
            {
                "Alas": {
                    "Emulator": {
                        "Serial": "127.0.0.1:5556",
                        "PackageName": "com.example.azurpilot",
                    }
                },
                "General": {},
                "SyntheticTask": {"Scheduler": {}},
            }
        ),
        encoding="utf-8",
    )
    backend = _FakeRuntimeBackend()
    manager = _manager(environment, backend)
    control_id = _accepted(manager, ControlAction.START_GAME)

    DevTargetRegistry.configure(
        environment.repository_root,
        profile_name=target_b,
        explicit_consent=True,
    )

    result = manager.execute(control_id)

    assert result.ok is False
    assert result.code == "DEV_CONTROL_TARGET_CHANGED"
    assert result.details["control_operation"]["outcome"] == ControlOutcome.PRECONDITION_FAILED.value
    assert result.details["control_operation"]["transitions"][-1]["code"] == "DEV_CONTROL_TARGET_CHANGED"
    assert backend.calls == []


def test_control_operation_fails_closed_after_critical_config_change(
    tmp_path: Path,
    supervisor_identity: None,
) -> None:
    environment = _environment(tmp_path)
    backend = _FakeRuntimeBackend()
    manager = _manager(environment, backend)
    control_id = _accepted(manager, ControlAction.START_GAME)
    profile_path = environment.profile_file
    payload = json.loads(profile_path.read_text(encoding="utf-8"))
    payload["Alas"]["Emulator"]["Serial"] = "127.0.0.1:5556"
    profile_path.write_text(json.dumps(payload), encoding="utf-8")

    result = manager.execute(control_id)

    assert result.ok is False
    assert result.code == "DEV_CONTROL_CONFIG_CHANGED"
    assert result.details["control_operation"]["outcome"] == ControlOutcome.PRECONDITION_FAILED.value
    assert result.details["control_operation"]["transitions"][-1]["code"] == "DEV_CONTROL_CONFIG_CHANGED"
    assert backend.calls == []


def test_control_operation_without_target_identity_is_corrupt(
    tmp_path: Path,
    supervisor_identity: None,
) -> None:
    environment = _environment(tmp_path)
    manager = _manager(environment, _FakeRuntimeBackend())
    control_id = _accepted(manager, ControlAction.START_GAME)
    payload = json.loads(manager.store.operation_path.read_text(encoding="utf-8"))
    payload.pop("target_identity")
    manager.store.operation_path.write_text(json.dumps(payload), encoding="utf-8")

    result = manager.execute(control_id)

    assert result.ok is False
    assert result.code == "DEV_CONTROL_STATE_CORRUPT"


def test_control_config_fingerprint_changes_when_profile_changes(
    tmp_path: Path,
    supervisor_identity: None,
) -> None:
    environment = _environment(tmp_path)
    before = control_module.runtime_config_fingerprint(environment)
    payload = json.loads(environment.profile_file.read_text(encoding="utf-8"))
    payload["Alas"]["Emulator"]["PackageName"] = "com.example.changed"
    environment.profile_file.write_text(json.dumps(payload), encoding="utf-8")

    assert control_module.runtime_config_fingerprint(environment) != before


def test_configured_backend_invalidates_cached_configuration_on_profile_change(
    tmp_path: Path,
    supervisor_identity: None,
) -> None:
    environment = _environment(tmp_path)
    backend = ConfiguredRuntimeBackend(environment)
    assert backend._configuration() == ("127.0.0.1:5555", "com.example.azurpilot")
    backend._platform = object()
    backend._app = object()

    payload = json.loads(environment.profile_file.read_text(encoding="utf-8"))
    payload["Alas"]["Emulator"]["Serial"] = "127.0.0.1:5556"
    environment.profile_file.write_text(json.dumps(payload), encoding="utf-8")

    assert backend._configuration() == ("127.0.0.1:5556", "com.example.azurpilot")
    assert backend._platform is None
    assert backend._app is None


def test_supervisor_crash_is_reconciled_as_aborted(
    tmp_path: Path,
    supervisor_identity: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment(tmp_path)
    manager = _manager(environment, _FakeRuntimeBackend())
    control_id = _accepted(manager, ControlAction.START_GAME)
    _mark_supervisor_claimed(manager, control_id)
    monkeypatch.setattr(control_module, "_process_matches", lambda _pid, _created_at: False)

    result = manager.get_operation(control_id)

    assert result.ok is False
    assert result.details["control_operation"]["outcome"] == ControlOutcome.ABORTED.value
    assert result.details["control_operation"]["transitions"][-1]["code"] == "DEV_CONTROL_SUPERVISOR_CRASHED"


def test_corrupt_control_state_is_not_treated_as_missing_or_success(
    tmp_path: Path,
    supervisor_identity: None,
) -> None:
    environment = _environment(tmp_path)
    manager = _manager(environment, _FakeRuntimeBackend())
    manager.store._ensure_root()
    manager.store.operation_path.write_text("{broken", encoding="utf-8")

    result = manager.get_operation("a" * 32)

    assert result.ok is False
    assert result.code == "DEV_CONTROL_STATE_CORRUPT"


def test_control_state_rejects_unbounded_supervisor_timestamp(
    tmp_path: Path,
    supervisor_identity: None,
) -> None:
    environment = _environment(tmp_path)
    manager = _manager(environment, _FakeRuntimeBackend())
    _accepted(manager, ControlAction.START_GAME)
    payload = json.loads(manager.store.operation_path.read_text(encoding="utf-8"))
    payload["supervisor_created_at"] = 10**1000
    manager.store.operation_path.write_text(json.dumps(payload), encoding="utf-8")

    result = manager.get_operation(payload["control_id"])

    assert result.ok is False
    assert result.code == "DEV_CONTROL_STATE_CORRUPT"
