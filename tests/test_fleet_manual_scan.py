from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from module.application.fleet_manual_scan import (
    FLEET_MANUAL_SCAN_SOURCE,
    FleetManualScanCommand,
    FleetManualScanCoordinator,
    FleetManualScanStatus,
)
from module.application.fleet_state import FleetScanBatchResult
from module.formation.model import FleetSelection


def _command(status=FleetManualScanStatus.PENDING):
    now = datetime(2026, 8, 25, tzinfo=UTC)
    kwargs = {}
    if status is not FleetManualScanStatus.PENDING:
        kwargs["started_at"] = now
    return FleetManualScanCommand(
        id=uuid4(),
        instance_id=uuid4(),
        selection=FleetSelection.several(1, 3),
        created_at=now,
        status=status,
        **kwargs,
    )


class _Commands:
    def __init__(self, command):
        self.command = command
        self.events = []

    def recover_interrupted(self, instance):
        self.events.append(("recover", instance))
        return 0

    def pending_exists(self, instance):
        self.events.append(("pending", instance))
        return self.command is not None

    def claim_next(self, instance):
        self.events.append(("claim", instance))
        command, self.command = self.command, None
        if command is None:
            return None
        return FleetManualScanCommand(
            id=command.id,
            instance_id=command.instance_id,
            selection=command.selection,
            created_at=command.created_at,
            started_at=command.created_at,
            status=FleetManualScanStatus.RUNNING,
        )

    def finish(
        self,
        instance,
        command_id,
        *,
        status,
        result_run_id,
        error_code,
    ):
        self.events.append(
            ("finish", instance, command_id, status, result_run_id, error_code)
        )
        now = datetime(2026, 8, 25, tzinfo=UTC)
        return FleetManualScanCommand(
            id=command_id,
            instance_id=uuid4(),
            selection=FleetSelection.several(1, 3),
            created_at=now,
            started_at=now,
            finished_at=now,
            status=status,
            result_run_id=result_run_id,
            error_code=error_code,
        )


class _State:
    def __init__(self, batch=None, error=None):
        self.batch = batch
        self.error = error
        self.calls = []

    def scan(self, instance, selection, *, source):
        self.calls.append((instance, selection, source))
        if self.error is not None:
            raise self.error
        return self.batch


def _batch(*, failure_code=None):
    selection = FleetSelection.several(1, 3)
    return FleetScanBatchResult(
        run_id=uuid4(),
        selection=selection,
        observations=(),
        failed_fleet_index=1 if failure_code else None,
        failure_code=failure_code,
    )


def test_manual_coordinator_recovers_claims_and_uses_manual_source() -> None:
    commands = _Commands(_command())
    batch = _batch(failure_code="physical_scan_failed")
    state = _State(batch)
    coordinator = FleetManualScanCoordinator(commands, state)

    execution = coordinator.process_next("profile-a")

    assert execution.command.status is FleetManualScanStatus.FAILED
    assert state.calls == [
        ("profile-a", FleetSelection.several(1, 3), FLEET_MANUAL_SCAN_SOURCE)
    ]
    assert commands.events[0] == ("recover", "profile-a")
    assert commands.events[1] == ("claim", "profile-a")
    assert commands.events[-1][3] is FleetManualScanStatus.FAILED
    assert commands.events[-1][-1] == "physical_scan_failed"


def test_recovery_happens_once_and_pending_check_is_worker_wakeup_path() -> None:
    commands = _Commands(_command())
    coordinator = FleetManualScanCoordinator(commands, _State())

    assert coordinator.has_pending("profile-a")
    assert coordinator.has_pending("profile-a")

    assert commands.events.count(("recover", "profile-a")) == 1
    assert commands.events.count(("pending", "profile-a")) == 2


def test_persistence_or_scan_failure_never_becomes_success() -> None:
    commands = _Commands(_command())
    state = _State(error=RuntimeError("database unavailable"))
    coordinator = FleetManualScanCoordinator(commands, state)

    with pytest.raises(RuntimeError, match="database unavailable"):
        coordinator.process_next("profile-a")

    finish = commands.events[-1]
    assert finish[0] == "finish"
    assert finish[3] is FleetManualScanStatus.FAILED
    assert finish[4] is None
    assert finish[5] == "manual_scan_failed"


def test_no_pending_command_does_not_touch_device_state_service() -> None:
    commands = _Commands(None)
    state = _State()
    coordinator = FleetManualScanCoordinator(commands, state)

    assert coordinator.process_next("profile-a") is None
    assert state.calls == []
