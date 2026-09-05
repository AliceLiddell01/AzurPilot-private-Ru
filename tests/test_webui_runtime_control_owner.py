from __future__ import annotations

import json
from pathlib import Path

from module.application.runtime_control import (
    RuntimeControlOperation,
    RuntimeOwnerIdentity,
)
from module.application.runtime_handover import NotificationOutcome
from module.application.runtime_state import RuntimePhase
from module.application.scheduler_runtime import SchedulerRuntimeStateReader
from module.webui.runtime_control_owner import WebUIRuntimeControlOwner

_EXPIRY = "2099-01-01T00:00:00+00:00"


class Manager:
    def __init__(self) -> None:
        self.alive = False
        self.calls: list[tuple[str, str | None, str | None]] = []

    def start(
        self,
        func: str | None,
        ev: object | None = None,
        *,
        operation_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        self.calls.append(("start", operation_id, session_id))
        self.alive = True

    def stop(self) -> bool:
        self.calls.append(("stop", None, None))
        self.alive = False
        return True


class HandoverManager(Manager):
    def request_cooperative_stop(self, *, operation_id: str, session_id: str | None) -> bool:
        self.calls.append(("cooperative_stop", operation_id, session_id))
        return True

    def wait_for_exit(self, timeout_seconds: float) -> bool:
        self.calls.append(("wait_for_exit", str(timeout_seconds), None))
        self.alive = False
        return True


class FailedStartManager(Manager):
    def start(
        self,
        func: str | None,
        ev: object | None = None,
        *,
        operation_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        super().start(
            func,
            ev,
            operation_id=operation_id,
            session_id=session_id,
        )
        raise RuntimeError("synthetic start failure after worker launch")

    def request_cooperative_stop(
        self,
        *,
        operation_id: str,
        session_id: str | None,
    ) -> bool:
        self.calls.append(("cooperative_stop", operation_id, session_id))
        return True

    def wait_for_exit(self, timeout_seconds: float) -> bool:
        self.calls.append(("wait_for_exit", str(timeout_seconds), None))
        return False


class FailedStopManager(Manager):
    def stop(self) -> bool:
        self.calls.append(("stop", None, None))
        raise RuntimeError("synthetic stop failure")


class Application:
    def __init__(self) -> None:
        self.returned_to_main = 0

    def return_to_main(self, profile: str) -> bool:
        self.returned_to_main += 1
        return True

    def is_in_main(self, profile: str) -> bool:
        return True


def _owner(tmp_path: Path, manager: Manager) -> WebUIRuntimeControlOwner:
    owner_identity = RuntimeOwnerIdentity(pid=100, created_at=200.0)
    record = {"pid": 101, "created_at": 201.0}
    instance_owner = WebUIRuntimeControlOwner(
        tmp_path,
        manager_factory=lambda _profile: manager,
        profile_provider=lambda: ("ap",),
        worker_record_provider=lambda _profile: record if manager.alive else None,
        function_factory=lambda _profile: "alas",
        development_profile_provider=lambda: "ap",
    )
    instance_owner.owner_identity = lambda: owner_identity  # type: ignore[method-assign]
    instance_owner.owner_matches = lambda _owner: True  # type: ignore[method-assign]
    return instance_owner


def test_owner_executes_ap_inside_existing_webui_and_repeats_start_idempotently(
    tmp_path: Path,
) -> None:
    manager = Manager()
    owner = _owner(tmp_path, manager)

    started = owner.execute(
        RuntimeControlOperation.START_PROFILE,
        "ap",
        request_id="request-1",
        idempotency_key="key-1",
        session_id="session-1",
        expires_at=_EXPIRY,
    )
    repeated = owner.execute(
        RuntimeControlOperation.START_PROFILE,
        "ap",
        request_id="request-2",
        idempotency_key="key-2",
        session_id="session-1",
        expires_at=_EXPIRY,
    )

    assert started.ok is True
    assert started.code == "RUNTIME_STARTED"
    assert repeated.ok is True
    assert repeated.code == "RUNTIME_ALREADY_RUNNING"
    assert manager.calls == [("start", "request-1", "session-1")]
    assert owner.state.read("ap").phase is RuntimePhase.RESOURCE_READY

    stopped = owner.execute(
        RuntimeControlOperation.STOP_PROFILE,
        "ap",
        request_id="request-3",
        idempotency_key="key-3",
        session_id="session-1",
        expires_at=_EXPIRY,
    )
    assert stopped.ok is True
    assert stopped.code == "RUNTIME_STOPPED"
    assert manager.calls[-1][0] == "stop"
    assert owner.state.read("ap").phase is RuntimePhase.STOPPED


def test_owner_records_unconfirmed_stop_when_manager_stop_raises(tmp_path: Path) -> None:
    manager = FailedStopManager()
    owner = _owner(tmp_path, manager)

    started = owner.execute(
        RuntimeControlOperation.START_PROFILE,
        "ap",
        request_id="start-before-stop-failure",
        idempotency_key="start-before-stop-failure-key",
        session_id="session-1",
        expires_at=_EXPIRY,
    )
    result = owner.execute(
        RuntimeControlOperation.STOP_PROFILE,
        "ap",
        request_id="stop-failure",
        idempotency_key="stop-failure-key",
        session_id="session-1",
        expires_at=_EXPIRY,
    )

    assert started.ok is True
    assert result.ok is False
    assert result.code == "RUNTIME_STOP_UNCONFIRMED"
    assert result.details == {"stop_returned": None, "error": "RuntimeError"}
    snapshot = owner.state.read("ap")
    assert snapshot is not None
    assert snapshot.phase is RuntimePhase.FAILED
    assert snapshot.terminal_state == "stop_unconfirmed"
    assert snapshot.worker_running is True


def test_owner_rejects_development_start_without_session_before_manager_lookup(
    tmp_path: Path,
) -> None:
    manager_lookups: list[str] = []
    owner = WebUIRuntimeControlOwner(
        tmp_path,
        manager_factory=lambda profile: manager_lookups.append(profile) or Manager(),
        profile_provider=lambda: ("ap",),
        development_profile_provider=lambda: "ap",
    )
    owner_identity = RuntimeOwnerIdentity(pid=100, created_at=200.0)
    owner.owner_identity = lambda: owner_identity  # type: ignore[method-assign]
    owner.owner_matches = lambda _owner: True  # type: ignore[method-assign]

    result = owner.execute(
        RuntimeControlOperation.START_PROFILE,
        "ap",
        request_id="missing-session",
        idempotency_key="missing-session-key",
        session_id=None,
        expires_at=_EXPIRY,
    )

    assert result.ok is False
    assert result.code == "RUNTIME_SESSION_REQUIRED"
    assert manager_lookups == []


def test_owner_rejects_expired_start_before_manager_lookup(tmp_path: Path) -> None:
    manager = Manager()
    owner = _owner(tmp_path, manager)

    result = owner.execute(
        RuntimeControlOperation.START_PROFILE,
        "ap",
        request_id="expired-start",
        idempotency_key="expired-start-key",
        session_id="session-1",
        expires_at="2000-01-01T00:00:00+00:00",
    )

    assert result.ok is False
    assert result.code == "RUNTIME_CONTROL_EXPIRED"
    assert manager.calls == []


def test_owner_uses_bounded_default_for_invalid_handover_grace(tmp_path: Path) -> None:
    owner = _owner(tmp_path, Manager())
    owner._deploy_config_provider = lambda: type(
        "DeployConfig", (), {"RuntimeHandoverGraceSeconds": -1}
    )()

    assert owner._grace_seconds() == 30.0


def test_owner_rejects_development_stop_without_session_before_manager_lookup(
    tmp_path: Path,
) -> None:
    manager_lookups: list[str] = []
    owner = WebUIRuntimeControlOwner(
        tmp_path,
        manager_factory=lambda profile: manager_lookups.append(profile) or Manager(),
        profile_provider=lambda: ("ap",),
        development_profile_provider=lambda: "ap",
    )
    owner_identity = RuntimeOwnerIdentity(pid=100, created_at=200.0)
    owner.owner_identity = lambda: owner_identity  # type: ignore[method-assign]
    owner.owner_matches = lambda _owner: True  # type: ignore[method-assign]

    result = owner.execute(
        RuntimeControlOperation.STOP_PROFILE,
        "ap",
        request_id="missing-stop-session",
        idempotency_key="missing-stop-session-key",
        session_id=None,
        expires_at=_EXPIRY,
    )

    assert result.ok is False
    assert result.code == "RUNTIME_SESSION_REQUIRED"
    assert manager_lookups == []


def test_owner_escalates_after_cooperative_start_cleanup_timeout(tmp_path: Path) -> None:
    manager = FailedStartManager()
    owner = _owner(tmp_path, manager)
    owner._deploy_config_provider = lambda: type(
        "DeployConfig", (), {"RuntimeHandoverGraceSeconds": 30}
    )()

    result = owner.execute(
        RuntimeControlOperation.START_PROFILE,
        "ap",
        request_id="start-failure",
        idempotency_key="start-failure-key",
        session_id="session-1",
        expires_at=_EXPIRY,
    )

    assert result.ok is False
    assert result.code == "RUNTIME_START_UNCONFIRMED"
    assert result.details["cleanup_confirmed"] is True
    assert result.details["cleanup_escalation_attempted"] is True
    assert [call[0] for call in manager.calls] == [
        "start",
        "cooperative_stop",
        "wait_for_exit",
        "stop",
    ]
    assert manager.alive is False


def test_owner_does_not_take_over_development_worker_of_another_session(
    tmp_path: Path,
) -> None:
    manager = Manager()
    owner = _owner(tmp_path, manager)

    first = owner.execute(
        RuntimeControlOperation.START_PROFILE,
        "ap",
        request_id="request-1",
        idempotency_key="key-1",
        session_id="session-1",
        expires_at=_EXPIRY,
    )
    second = owner.execute(
        RuntimeControlOperation.START_PROFILE,
        "ap",
        request_id="request-2",
        idempotency_key="key-2",
        session_id="session-2",
        expires_at=_EXPIRY,
    )

    assert first.ok is True
    assert second.ok is False
    assert second.code == "RUNTIME_OWNERSHIP_MISMATCH"
    assert manager.calls == [("start", "request-1", "session-1")]


def test_owner_does_not_adopt_running_worker_without_runtime_state(
    tmp_path: Path,
) -> None:
    manager = Manager()
    manager.alive = True
    owner = _owner(tmp_path, manager)

    result = owner.execute(
        RuntimeControlOperation.START_PROFILE,
        "ap",
        request_id="request-unknown-state",
        idempotency_key="key-unknown-state",
        session_id="dev-session",
        expires_at=_EXPIRY,
    )

    assert result.ok is False
    assert result.code == "RUNTIME_STATE_UNKNOWN"
    assert manager.calls == []


def test_owner_rejects_session_mismatch_for_running_non_development_worker(
    tmp_path: Path,
) -> None:
    manager = Manager()
    manager.alive = True
    owner_identity = RuntimeOwnerIdentity(pid=100, created_at=200.0)
    record = {"pid": 101, "created_at": 201.0}
    owner = WebUIRuntimeControlOwner(
        tmp_path,
        manager_factory=lambda _profile: manager,
        profile_provider=lambda: ("user", "ap"),
        worker_record_provider=lambda _profile: record if manager.alive else None,
        function_factory=lambda _profile: "alas",
        development_profile_provider=lambda: "ap",
    )
    owner.owner_identity = lambda: owner_identity  # type: ignore[method-assign]
    owner.owner_matches = lambda _owner: True  # type: ignore[method-assign]
    owner.state.mark_worker_started(
        "user",
        worker_pid=201,
        worker_created_at=301.0,
        session_id="session-1",
    )

    result = owner.execute(
        RuntimeControlOperation.START_PROFILE,
        "user",
        request_id="request-session-mismatch",
        idempotency_key="key-session-mismatch",
        session_id="session-2",
        expires_at=_EXPIRY,
    )

    assert result.ok is False
    assert result.code == "RUNTIME_OWNERSHIP_MISMATCH"
    assert manager.calls == []


def test_owner_handover_fails_closed_when_authoritative_state_is_missing(
    tmp_path: Path,
) -> None:
    user = HandoverManager()
    development = Manager()
    managers = {"alas": user, "ap": development}
    records = {"alas": {"pid": 201, "created_at": 301.0}}
    owner_identity = RuntimeOwnerIdentity(pid=100, created_at=200.0)
    owner = WebUIRuntimeControlOwner(
        tmp_path,
        manager_factory=lambda profile: managers[profile],
        profile_provider=lambda: ("alas", "ap"),
        worker_record_provider=lambda profile: records.get(profile)
        if managers[profile].alive
        else None,
        function_factory=lambda _profile: "alas",
        application=Application(),
        development_profile_provider=lambda: "ap",
    )
    owner.owner_identity = lambda: owner_identity  # type: ignore[method-assign]
    owner.owner_matches = lambda _owner: True  # type: ignore[method-assign]
    user.alive = True

    result = owner.execute(
        RuntimeControlOperation.START_PROFILE,
        "ap",
        request_id="handover-unknown",
        idempotency_key="handover-unknown-key",
        session_id="dev-session",
        expires_at=_EXPIRY,
    )

    assert result.ok is False
    assert result.code == "RUNTIME_HANDOVER_STATE_UNKNOWN"
    assert user.calls == []
    assert development.calls == []


def test_owner_keeps_machine_catalog_and_rejects_unknown_profile(
    tmp_path: Path,
) -> None:
    manager = Manager()
    owner = _owner(tmp_path, manager)
    assert owner._profiles() == ("ap",)

    result = owner.execute(
        RuntimeControlOperation.START_PROFILE,
        "unknown",
        request_id="request-4",
        idempotency_key="key-4",
        session_id=None,
        expires_at=_EXPIRY,
    )
    assert result.ok is False
    assert result.code == "RUNTIME_PROFILE_INVALID"


def test_owner_handover_warns_and_uses_cooperative_stop_before_ap_start(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    config_path = config / "alas.json"
    config_path.write_text(
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
    scheduler_reader = SchedulerRuntimeStateReader(tmp_path)
    scheduler_fingerprint = scheduler_reader.semantic_fingerprint(
        "alas", ("DailyTask", "WeeklyTask")
    )
    scheduler_config = config_path.read_bytes()

    user = HandoverManager()
    development = Manager()
    managers = {"alas": user, "ap": development}
    records = {
        "alas": {"pid": 201, "created_at": 301.0},
        "ap": {"pid": 202, "created_at": 302.0},
    }
    application = Application()
    notifications: list[tuple[str, str, str]] = []
    owner_identity = RuntimeOwnerIdentity(pid=100, created_at=200.0)
    owner = WebUIRuntimeControlOwner(
        tmp_path,
        manager_factory=lambda profile: managers[profile],
        profile_provider=lambda: ("alas", "ap"),
        worker_record_provider=lambda profile: records[profile] if managers[profile].alive else None,
        function_factory=lambda _profile: "alas",
        application=application,
        notifier=lambda profile, title, content: notifications.append((profile, title, content))
        or NotificationOutcome.DELIVERED,
        deploy_config_provider=lambda: type("DeployConfig", (), {"RuntimeHandoverGraceSeconds": 0})(),
        development_profile_provider=lambda: "ap",
    )
    owner.owner_identity = lambda: owner_identity  # type: ignore[method-assign]
    owner.owner_matches = lambda _owner: True  # type: ignore[method-assign]
    owner.state.mark_worker_started("alas", worker_pid=201, worker_created_at=301.0)
    owner.state.mark_task_started("alas", "DailyTask", operation_id="user-task")
    user.alive = True

    result = owner.execute(
        RuntimeControlOperation.START_PROFILE,
        "ap",
        request_id="handover-1",
        idempotency_key="handover-key",
        session_id="dev-session",
        expires_at=_EXPIRY,
    )

    assert result.ok is True
    assert result.code == "RUNTIME_STARTED"
    handover = result.details["handover"]
    assert handover["phases"] == [
        "handover_requested",
        "preemption_notice",
        "grace_period",
        "quiesce_requested",
        "current_task_draining",
        "current_task_stopped",
        "returning_to_main",
        "main_confirmed",
    ]
    assert handover["details"]["notification"] == {
        "attempted": True,
        "outcome": "delivered",
        "confirmed": True,
    }
    assert notifications and notifications[0][0] == "alas"
    assert [call[0] for call in user.calls] == ["cooperative_stop", "wait_for_exit"]
    assert application.returned_to_main == 1
    assert development.calls == [("start", "handover-1", "dev-session")]
    assert scheduler_reader.semantic_fingerprint("alas", ("DailyTask", "WeeklyTask")) == scheduler_fingerprint
    assert config_path.read_bytes() == scheduler_config
