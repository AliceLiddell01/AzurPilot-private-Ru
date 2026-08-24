from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from module.application.fleet_autoscan import (
    FLEET_AUTOSCAN_SOURCE_DAILY,
    FLEET_AUTOSCAN_SOURCE_EVERY_START,
    FleetAutoScanConfig,
    FleetAutoScanCoordinator,
    FleetAutoScanMode,
    FleetAutoScanPolicy,
    FleetAutoScanRetryPolicy,
)
from module.application.fleet_state import (
    FleetScanAttempt,
    FleetScanBatchResult,
    FleetScanRunStatus,
    FleetStateObservation,
)
from module.dock_inventory.model import CanonicalShipIdentity, IdentityStatus
from module.formation.model import (
    FleetSelection,
    FormationFleetSide,
    FormationFleetSlotObservation,
    FormationFleetSnapshot,
)


def _snapshot(fleet_index: int, *, complete: bool) -> FormationFleetSnapshot:
    status = IdentityStatus.MATCHED if complete else IdentityStatus.UNRESOLVED
    occupied = FormationFleetSlotObservation(
        side=FormationFleetSide.MAIN,
        position=1,
        occupied=True,
        identity_status=status,
        raw_name_ocr="Enterprise",
        displayed_name="Enterprise",
        canonical_identity=(
            CanonicalShipIdentity("azur_lane_ship_group:1") if complete else None
        ),
        canonical_name="Enterprise" if complete else None,
    )
    empty = tuple(
        FormationFleetSlotObservation(side=side, position=position, occupied=False)
        for side, position in (
            (FormationFleetSide.MAIN, 2),
            (FormationFleetSide.MAIN, 3),
            (FormationFleetSide.VANGUARD, 1),
            (FormationFleetSide.VANGUARD, 2),
            (FormationFleetSide.VANGUARD, 3),
        )
    )
    return FormationFleetSnapshot(
        fleet_index=fleet_index,
        slots=(occupied, *empty),
        catalog_fingerprint="a" * 64,
    )


def _batch(
    selection: FleetSelection,
    *,
    complete: tuple[int, ...] = (),
    incomplete: tuple[int, ...] = (),
    failed: int | None = None,
) -> FleetScanBatchResult:
    run_id = uuid4()
    observations = tuple(
        FleetStateObservation(
            id=uuid4(),
            run_id=run_id,
            instance_id=uuid4(),
            idempotency_key=f"test:{run_id}:{fleet_index}",
            observed_at=datetime(2026, 8, 25, 12, tzinfo=UTC),
            snapshot=_snapshot(fleet_index, complete=fleet_index in complete),
        )
        for fleet_index in sorted((*complete, *incomplete))
    )
    return FleetScanBatchResult(
        run_id=run_id,
        selection=selection,
        observations=observations,
        failed_fleet_index=failed,
        failure_code="physical_scan_failed" if failed is not None else None,
    )


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class _StateService:
    def __init__(self) -> None:
        self.complete: set[int] = set()
        self.attempts: tuple[FleetScanAttempt, ...] = ()
        self.calls: list[tuple] = []
        self.result_factory = lambda selection: _batch(
            selection,
            complete=selection.fleet_indices,
        )

    def complete_in_window(self, instance, selection, *, start, end):
        self.calls.append(("complete", instance, selection.fleet_indices, start, end))
        return tuple(index for index in selection.fleet_indices if index in self.complete)

    def latest_attempts(self, instance, selection, *, source):
        self.calls.append(("attempts", instance, selection.fleet_indices, source))
        return tuple(
            attempt
            for attempt in self.attempts
            if attempt.fleet_index in selection.fleet_indices and attempt.source == source
        )

    def scan(self, instance, selection, *, source):
        self.calls.append(("scan", instance, selection.fleet_indices, source))
        result = self.result_factory(selection)
        self.complete.update(
            observation.fleet_index
            for observation in result.observations
            if observation.snapshot.complete
        )
        return result


def _config(
    mode: FleetAutoScanMode,
    *fleet_indices: int,
) -> FleetAutoScanConfig:
    return FleetAutoScanConfig(mode, FleetSelection(tuple(fleet_indices)))


def _attempt(
    fleet_index: int,
    started_at: datetime,
    *,
    source: str = FLEET_AUTOSCAN_SOURCE_DAILY,
    status: FleetScanRunStatus = FleetScanRunStatus.FAILED,
) -> FleetScanAttempt:
    return FleetScanAttempt(
        run_id=uuid4(),
        fleet_index=fleet_index,
        source=source,
        started_at=started_at,
        status=status,
        error_code=(
            None
            if status in {FleetScanRunStatus.STARTED, FleetScanRunStatus.SUCCEEDED}
            else "physical_scan_failed"
        ),
    )


def test_config_normalizes_selection_and_rejects_invalid_values() -> None:
    config = FleetAutoScanConfig.from_raw("daily", [6, 2, 6, 1])
    assert config.mode is FleetAutoScanMode.DAILY
    assert config.selection.fleet_indices == (1, 2, 6)

    with pytest.raises(ValueError):
        FleetAutoScanConfig.from_raw("sometimes", [1])
    with pytest.raises(TypeError):
        FleetAutoScanConfig.from_raw("daily", "1,2")
    with pytest.raises(ValueError):
        FleetAutoScanConfig.from_raw("daily", [])
    with pytest.raises(ValueError):
        FleetAutoScanConfig.from_raw("daily", [1, 7])


def test_disabled_does_not_touch_storage_or_scan() -> None:
    state = _StateService()
    coordinator = FleetAutoScanCoordinator(
        state,
        FleetAutoScanPolicy(ZoneInfo("Asia/Novosibirsk")),
    )

    assert coordinator.run_if_due("profile", _config(FleetAutoScanMode.DISABLED, 1, 2)) is None
    assert state.calls == []


def test_calendar_day_uses_runtime_timezone_and_half_open_utc_bounds() -> None:
    policy = FleetAutoScanPolicy(ZoneInfo("Asia/Novosibirsk"))

    window = policy.calendar_day_window(datetime(2026, 8, 25, 20, tzinfo=UTC))

    assert window.start == datetime(2026, 8, 25, 17, tzinfo=UTC)
    assert window.end == datetime(2026, 8, 26, 17, tzinfo=UTC)


def test_daily_scans_only_unsatisfied_subset_and_uses_daily_source() -> None:
    state = _StateService()
    state.complete.update({1, 3})
    clock = _Clock(datetime(2026, 8, 25, 12, tzinfo=UTC))
    coordinator = FleetAutoScanCoordinator(
        state,
        FleetAutoScanPolicy(ZoneInfo("Asia/Novosibirsk")),
        clock=clock,
    )

    result = coordinator.run_if_due(
        "profile",
        _config(FleetAutoScanMode.DAILY, 1, 2, 3, 4),
    )

    assert result is not None
    assert result.due_selection.fleet_indices == (2, 4)
    assert state.calls[-1] == (
        "scan",
        "profile",
        (2, 4),
        FLEET_AUTOSCAN_SOURCE_DAILY,
    )


def test_daily_complete_remains_satisfied_after_newer_incomplete_fact() -> None:
    state = _StateService()
    state.complete.add(2)
    coordinator = FleetAutoScanCoordinator(
        state,
        FleetAutoScanPolicy(ZoneInfo("Asia/Novosibirsk")),
    )

    assert coordinator.run_if_due("profile", _config(FleetAutoScanMode.DAILY, 2)) is None
    assert not any(call[0] == "scan" for call in state.calls)


def test_daily_failed_attempt_survives_new_coordinator_and_retries_after_cooldown() -> None:
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    state = _StateService()
    state.attempts = (_attempt(4, now - timedelta(minutes=10)),)
    clock = _Clock(now)
    policy = FleetAutoScanPolicy(
        ZoneInfo("Asia/Novosibirsk"),
        FleetAutoScanRetryPolicy(timedelta(minutes=30)),
    )

    first_process = FleetAutoScanCoordinator(state, policy, clock=clock)
    assert first_process.run_if_due("profile", _config(FleetAutoScanMode.DAILY, 4)) is None

    clock.value = now + timedelta(minutes=21)
    restarted_process = FleetAutoScanCoordinator(state, policy, clock=clock)
    assert restarted_process.run_if_due("profile", _config(FleetAutoScanMode.DAILY, 4)) is not None


def test_daily_new_day_ignores_previous_day_cooldown() -> None:
    state = _StateService()
    state.attempts = (
        _attempt(1, datetime(2026, 8, 25, 16, 59, tzinfo=UTC)),
    )
    coordinator = FleetAutoScanCoordinator(
        state,
        FleetAutoScanPolicy(
            ZoneInfo("Asia/Novosibirsk"),
            FleetAutoScanRetryPolicy(timedelta(minutes=30)),
        ),
        clock=_Clock(datetime(2026, 8, 25, 17, 1, tzinfo=UTC)),
    )

    assert coordinator.run_if_due("profile", _config(FleetAutoScanMode.DAILY, 1)) is not None


def test_every_start_complete_is_not_repeated_but_incomplete_retries() -> None:
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    state = _StateService()
    state.result_factory = lambda selection: _batch(
        selection,
        complete=(1,),
        incomplete=(2,),
    )
    clock = _Clock(now)
    coordinator = FleetAutoScanCoordinator(
        state,
        FleetAutoScanPolicy(
            ZoneInfo("UTC"),
            FleetAutoScanRetryPolicy(timedelta(minutes=30)),
        ),
        clock=clock,
    )
    config = _config(FleetAutoScanMode.EVERY_START, 1, 2)

    first = coordinator.run_if_due("profile", config)
    assert first is not None
    assert first.complete_fleet_indices == (1,)
    assert first.incomplete_fleet_indices == (2,)
    assert coordinator.run_if_due("profile", config) is None

    state.result_factory = lambda selection: _batch(
        selection,
        complete=selection.fleet_indices,
    )
    clock.value += timedelta(minutes=30)
    retry = coordinator.run_if_due("profile", config)
    assert retry is not None
    assert retry.due_selection.fleet_indices == (2,)


def test_every_start_new_process_ignores_database_daily_satisfaction() -> None:
    state = _StateService()
    state.complete.update({1, 2})
    config = _config(FleetAutoScanMode.EVERY_START, 1, 2)
    policy = FleetAutoScanPolicy(ZoneInfo("UTC"))

    first = FleetAutoScanCoordinator(state, policy)
    assert first.run_if_due("profile", config) is not None
    assert first.run_if_due("profile", config) is None

    second = FleetAutoScanCoordinator(state, policy)
    result = second.run_if_due("profile", config)
    assert result is not None
    assert result.due_selection.fleet_indices == (1, 2)
    assert state.calls[-1][-1] == FLEET_AUTOSCAN_SOURCE_EVERY_START


def test_partial_batch_keeps_only_complete_fleets_satisfied() -> None:
    state = _StateService()
    state.result_factory = lambda selection: _batch(
        selection,
        complete=(1,),
        incomplete=(2,),
        failed=3,
    )
    clock = _Clock(datetime(2026, 8, 25, 12, tzinfo=UTC))
    coordinator = FleetAutoScanCoordinator(
        state,
        FleetAutoScanPolicy(
            ZoneInfo("UTC"),
            FleetAutoScanRetryPolicy(timedelta(minutes=5)),
        ),
        clock=clock,
    )
    config = _config(FleetAutoScanMode.EVERY_START, 1, 2, 3)

    first = coordinator.run_if_due("profile", config)
    assert first is not None
    assert first.batch_result.status is FleetScanRunStatus.PARTIAL
    clock.value += timedelta(minutes=5)
    state.result_factory = lambda selection: _batch(
        selection,
        complete=selection.fleet_indices,
    )
    retry = coordinator.run_if_due("profile", config)
    assert retry is not None
    assert retry.due_selection.fleet_indices == (2, 3)


def test_retry_policy_rejects_zero_or_unbounded_cooldown() -> None:
    with pytest.raises(ValueError):
        FleetAutoScanRetryPolicy(timedelta(0))
    with pytest.raises(ValueError):
        FleetAutoScanRetryPolicy(timedelta(hours=25))
    with pytest.raises(ValueError):
        FleetAutoScanRetryPolicy(
            timedelta(hours=25),
            maximum_cooldown=timedelta(hours=48),
        )
