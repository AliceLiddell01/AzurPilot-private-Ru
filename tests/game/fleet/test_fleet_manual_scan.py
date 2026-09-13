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
from module.application.fleet_state import FleetScanBatchResult, FleetStateObservation
from module.formation.model import (
    FleetSelection,
    FormationFleetSide,
    FormationFleetSlotObservation,
    FormationFleetSnapshot,
)


def _command(
    status=FleetManualScanStatus.PENDING,
    selection: FleetSelection | None = None,
):
    now = datetime(2026, 8, 25, tzinfo=UTC)
    kwargs = {}
    if status is not FleetManualScanStatus.PENDING:
        kwargs["started_at"] = now
    return FleetManualScanCommand(
        id=uuid4(),
        instance_id=uuid4(),
        selection=selection or FleetSelection.several(1, 3),
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


class _FinishFailsOnceCommands(_Commands):
    def __init__(self, command):
        super().__init__(command)
        self._finish_failed = False

    def finish(
        self,
        instance,
        command_id,
        *,
        status,
        result_run_id,
        error_code,
    ):
        if not self._finish_failed:
            self._finish_failed = True
            self.events.append(("finish-error", instance, command_id))
            raise RuntimeError("database unavailable")
        return super().finish(
            instance,
            command_id,
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


def _snapshot(fleet_index: int) -> FormationFleetSnapshot:
    coordinates = (
        (FormationFleetSide.MAIN, 1),
        (FormationFleetSide.MAIN, 2),
        (FormationFleetSide.MAIN, 3),
        (FormationFleetSide.VANGUARD, 1),
        (FormationFleetSide.VANGUARD, 2),
        (FormationFleetSide.VANGUARD, 3),
    )
    return FormationFleetSnapshot(
        fleet_index=fleet_index,
        slots=tuple(
            FormationFleetSlotObservation(
                side=side,
                position=position,
                occupied=False,
            )
            for side, position in coordinates
        ),
        catalog_fingerprint="e" * 64,
    )


def _observation(run_id, fleet_index: int) -> FleetStateObservation:
    return FleetStateObservation(
        id=uuid4(),
        run_id=run_id,
        instance_id=uuid4(),
        idempotency_key=f"manual-test:{fleet_index}",
        observed_at=datetime(2026, 8, 25, tzinfo=UTC),
        snapshot=_snapshot(fleet_index),
    )


def _batch(*, failure_code=None):
    selection = FleetSelection.several(1, 3)
    return FleetScanBatchResult(
        run_id=uuid4(),
        selection=selection,
        observations=(),
        failed_fleet_index=1 if failure_code else None,
        failure_code=failure_code,
    )


def _successful_batch(fleet_index: int = 1) -> FleetScanBatchResult:
    run_id = uuid4()
    selection = FleetSelection.one(fleet_index)
    return FleetScanBatchResult(
        run_id=run_id,
        selection=selection,
        observations=(_observation(run_id, fleet_index),),
    )


def _partial_batch() -> FleetScanBatchResult:
    run_id = uuid4()
    selection = FleetSelection.several(1, 3)
    return FleetScanBatchResult(
        run_id=run_id,
        selection=selection,
        observations=(_observation(run_id, 1),),
        failed_fleet_index=3,
        failure_code="physical_scan_failed",
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


def test_successful_batch_finishes_command_with_result_run_id() -> None:
    selection = FleetSelection.one(1)
    commands = _Commands(_command(selection=selection))
    batch = _successful_batch(1)
    state = _State(batch)
    coordinator = FleetManualScanCoordinator(commands, state)

    execution = coordinator.process_next("profile-a")

    assert execution.command.status is FleetManualScanStatus.SUCCEEDED
    assert execution.command.result_run_id == batch.run_id
    assert execution.command.error_code is None
    assert commands.events[-1][3] is FleetManualScanStatus.SUCCEEDED
    assert commands.events[-1][4] == batch.run_id
    assert commands.events[-1][5] is None


def test_partial_batch_finishes_command_with_result_run_id() -> None:
    selection = FleetSelection.several(1, 3)
    commands = _Commands(_command(selection=selection))
    batch = _partial_batch()
    state = _State(batch)
    coordinator = FleetManualScanCoordinator(commands, state)

    execution = coordinator.process_next("profile-a")

    assert execution.command.status is FleetManualScanStatus.PARTIAL
    assert execution.command.result_run_id == batch.run_id
    assert execution.command.error_code == "physical_scan_failed"
    assert commands.events[-1][3] is FleetManualScanStatus.PARTIAL
    assert commands.events[-1][4] == batch.run_id
    assert commands.events[-1][5] == "physical_scan_failed"


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


def test_terminal_persistence_failure_rearms_recovery_in_same_worker() -> None:
    commands = _FinishFailsOnceCommands(_command())
    coordinator = FleetManualScanCoordinator(
        commands,
        _State(_batch(failure_code="physical_scan_failed")),
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        coordinator.process_next("profile-a")

    assert commands.events.count(("recover", "profile-a")) == 1
    assert coordinator.has_pending("profile-a") is False
    assert commands.events.count(("recover", "profile-a")) == 2


def test_no_pending_command_does_not_touch_device_state_service() -> None:
    commands = _Commands(None)
    state = _State()
    coordinator = FleetManualScanCoordinator(commands, state)

    assert coordinator.process_next("profile-a") is None
    assert state.calls == []
