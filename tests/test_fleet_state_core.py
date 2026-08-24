from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from module.application.errors import StorageUnavailableError
from module.application.fleet_state import (
    FleetRefreshPolicy,
    FleetScanRunStatus,
    FleetScanService,
    FleetStateRequest,
    FleetStateService,
)
from module.application.instance_identity import runtime_instance_identity
from module.application.storage_models import InstanceIdentity
from module.dock_inventory.model import CanonicalShipIdentity, IdentityStatus
from module.formation.model import (
    SUPPORTED_SURFACE_FLEET_INDICES,
    FleetSelection,
    FormationFleetSide,
    FormationFleetSlotObservation,
    FormationFleetSnapshot,
)


def _snapshot(
    fleet_index: int,
    *,
    status: IdentityStatus = IdentityStatus.MATCHED,
) -> FormationFleetSnapshot:
    occupied = FormationFleetSlotObservation(
        side=FormationFleetSide.MAIN,
        position=1,
        occupied=True,
        identity_status=status,
        raw_name_ocr="Enterprise",
        displayed_name="Enterprise",
        canonical_identity=(
            CanonicalShipIdentity("azur_lane_ship_group:1")
            if status is IdentityStatus.MATCHED
            else None
        ),
        canonical_name="Enterprise" if status is IdentityStatus.MATCHED else None,
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


class _Instances:
    def __init__(self):
        self._by_alias: dict[tuple[str, str], InstanceIdentity] = {}

    def resolve(self, *, alias_kind, alias_digest):
        return self._by_alias.get((alias_kind, alias_digest))

    def register(
        self,
        identity,
        *,
        alias_kind,
        alias_digest,
        source_provenance,
    ):
        del source_provenance
        self._by_alias[(alias_kind, alias_digest)] = identity
        return True


class _FleetRepository:
    def __init__(self):
        self.runs = {}
        self.requests = {}
        self.statuses = {}
        self.observations = []
        self.fail_append_for: int | None = None

    def create_run(self, run):
        self.runs[run.id] = run
        self.requests[run.id] = run.selection.fleet_indices

    def append_observation(self, observation):
        if observation.fleet_index == self.fail_append_for:
            raise StorageUnavailableError("Хранилище недоступно.")
        self.observations.append(observation)
        return True

    def finish_run(self, run_id, *, status, finished_at, error_code):
        self.statuses[run_id] = (status, finished_at, error_code)

    def latest(self, instance_id, selection):
        latest = {}
        for observation in self.observations:
            if (
                observation.instance_id == instance_id
                and observation.fleet_index in selection.fleet_indices
            ):
                previous = latest.get(observation.fleet_index)
                if previous is None or (
                    observation.observed_at,
                    observation.id,
                ) > (previous.observed_at, previous.id):
                    latest[observation.fleet_index] = observation
        return tuple(latest[index] for index in sorted(latest))

    def history(self, instance_id, fleet_index, *, limit):
        matches = [
            item
            for item in self.observations
            if item.instance_id == instance_id and item.fleet_index == fleet_index
        ]
        return tuple(
            sorted(
                matches,
                key=lambda item: (item.observed_at, item.id),
                reverse=True,
            )[:limit]
        )


class _Uow:
    def __init__(self, instances, fleet_state):
        self.instances = instances
        self.fleet_state = fleet_state
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None

    def commit(self):
        self.committed = True

    def rollback(self):
        return None


class _Controller:
    def __init__(self, *, fail_at: int | None = None):
        self.fail_at = fail_at
        self.calls = []

    def scan_surface_fleet(self, fleet_index):
        self.calls.append(fleet_index)
        if fleet_index == self.fail_at:
            raise RuntimeError("небезопасное состояние UI")
        return _snapshot(fleet_index)


class _Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        current = self.value
        self.value += timedelta(seconds=1)
        return current


def _services(*, fail_at=None, now=None):
    instances = _Instances()
    repository = _FleetRepository()
    controller = _Controller(fail_at=fail_at)
    clock = _Clock(now or datetime(2026, 8, 25, tzinfo=UTC))

    def factory():
        return _Uow(instances, repository)

    scanner = FleetScanService(factory, controller, clock=clock)
    state = FleetStateService(factory, scanner, clock=clock)
    return repository, controller, clock, scanner, state


def test_selection_normalizes_one_several_duplicates_and_all():
    assert FleetSelection.one(3).fleet_indices == (3,)
    assert FleetSelection.several(6, 2, 6, 1).fleet_indices == (1, 2, 6)
    assert FleetSelection.all().fleet_indices == SUPPORTED_SURFACE_FLEET_INDICES


@pytest.mark.parametrize("value", [0, 7, True, False])
def test_selection_rejects_invalid_fleet(value):
    with pytest.raises(ValueError):
        FleetSelection((value,))


def test_selection_rejects_empty_and_mutable_input():
    with pytest.raises(ValueError):
        FleetSelection(())
    with pytest.raises(TypeError):
        FleetSelection([1])


def test_batch_scan_persists_all_requested_fleets_in_order():
    repository, controller, _, scanner, _ = _services()

    result = scanner.scan(
        "profile",
        FleetSelection.several(5, 2, 4),
        source="consumer:test",
    )

    assert controller.calls == [2, 4, 5]
    assert tuple(item.fleet_index for item in result.observations) == (2, 4, 5)
    assert result.status is FleetScanRunStatus.SUCCEEDED
    assert repository.requests[result.run_id] == (2, 4, 5)
    assert repository.statuses[result.run_id][0] is FleetScanRunStatus.SUCCEEDED


def test_batch_scan_all_uses_every_supported_fleet_once():
    _, controller, _, scanner, _ = _services()

    result = scanner.scan("profile", FleetSelection.all(), source="consumer:test")

    assert controller.calls == list(SUPPORTED_SURFACE_FLEET_INDICES)
    assert tuple(item.fleet_index for item in result.observations) == (
        SUPPORTED_SURFACE_FLEET_INDICES
    )


@pytest.mark.parametrize(
    ("fail_at", "expected_calls", "expected_success", "expected_status"),
    [
        (1, [1], (), FleetScanRunStatus.FAILED),
        (3, [1, 3], (1,), FleetScanRunStatus.PARTIAL),
    ],
)
def test_batch_scan_stops_fail_closed_and_keeps_prior_successes(
    fail_at,
    expected_calls,
    expected_success,
    expected_status,
):
    repository, controller, _, scanner, _ = _services(fail_at=fail_at)

    result = scanner.scan(
        "profile",
        FleetSelection.several(1, 3, 6),
        source="consumer:test",
    )

    assert controller.calls == expected_calls
    assert tuple(item.fleet_index for item in result.observations) == expected_success
    assert result.failed_fleet_index == fail_at
    assert result.failure_code == "physical_scan_failed"
    assert result.status is expected_status
    assert repository.statuses[result.run_id][0] is expected_status


def test_batch_scan_db_failure_is_not_reported_as_success():
    repository, controller, _, scanner, _ = _services()
    repository.fail_append_for = 2

    with pytest.raises(StorageUnavailableError):
        scanner.scan(
            "profile",
            FleetSelection.several(1, 2),
            source="consumer:test",
        )

    assert controller.calls == [1, 2]
    assert tuple(item.fleet_index for item in repository.observations) == (1,)
    assert next(iter(repository.statuses.values()))[0] is FleetScanRunStatus.PARTIAL
    assert next(iter(repository.statuses.values()))[2] == "persistence_failed"


def test_never_returns_saved_and_reports_missing_without_scan():
    repository, controller, _, _, state = _services()
    state.scan("profile", FleetSelection.one(1), source="seed")
    controller.calls.clear()

    result = state.state(
        "profile",
        FleetStateRequest(
            FleetSelection.several(1, 2, 3),
            FleetRefreshPolicy.NEVER,
        ),
        source="consumer:test",
    )

    assert tuple(item.fleet_index for item in result.observations) == (1,)
    assert result.missing_fleet_indices == (2, 3)
    assert result.refresh_result is None
    assert controller.calls == []
    assert len(repository.observations) == 1


def test_if_missing_scans_only_missing_from_mixed_selection():
    _, controller, _, _, state = _services()
    state.scan("profile", FleetSelection.one(2), source="seed")
    controller.calls.clear()

    result = state.state(
        "profile",
        FleetStateRequest(
            FleetSelection.several(1, 2, 4),
            FleetRefreshPolicy.IF_MISSING,
        ),
        source="consumer:test",
    )

    assert controller.calls == [1, 4]
    assert tuple(item.fleet_index for item in result.observations) == (1, 2, 4)
    assert result.missing_fleet_indices == ()


def test_if_missing_does_not_scan_when_everything_is_saved():
    _, controller, _, _, state = _services()
    state.scan("profile", FleetSelection.several(2, 4), source="seed")
    controller.calls.clear()

    result = state.state(
        "profile",
        FleetStateRequest(
            FleetSelection.several(2, 4),
            FleetRefreshPolicy.IF_MISSING,
        ),
        source="consumer:test",
    )

    assert controller.calls == []
    assert result.refresh_result is None
    assert result.missing_fleet_indices == ()


def test_if_stale_refreshes_only_expired_and_treats_boundary_as_fresh():
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    repository, controller, clock, _, state = _services(now=now - timedelta(hours=2))
    state.scan("profile", FleetSelection.several(1, 2), source="seed")
    first, second = repository.observations
    repository.observations[0] = type(first)(
        id=first.id,
        run_id=first.run_id,
        instance_id=first.instance_id,
        idempotency_key=first.idempotency_key,
        observed_at=now - timedelta(hours=1, seconds=1),
        snapshot=first.snapshot,
    )
    repository.observations[1] = type(second)(
        id=second.id,
        run_id=second.run_id,
        instance_id=second.instance_id,
        idempotency_key=second.idempotency_key,
        observed_at=now - timedelta(hours=1),
        snapshot=second.snapshot,
    )
    clock.value = now
    controller.calls.clear()

    result = state.state(
        "profile",
        FleetStateRequest(
            FleetSelection.several(1, 2, 3),
            FleetRefreshPolicy.IF_STALE,
            max_age=timedelta(hours=1),
        ),
        source="consumer:test",
    )

    assert controller.calls == [1, 3]
    assert result.refresh_result is not None
    assert tuple(item.fleet_index for item in result.observations) == (1, 2, 3)


def test_always_refreshes_fresh_saved_state_and_history_is_bounded():
    _, controller, _, _, state = _services()
    state.scan("profile", FleetSelection.one(4), source="seed")
    controller.calls.clear()

    result = state.state(
        "profile",
        FleetStateRequest(FleetSelection.one(4), FleetRefreshPolicy.ALWAYS),
        source="consumer:test",
    )

    assert controller.calls == [4]
    assert result.refresh_result is not None
    history = state.history("profile", 4, limit=1)
    assert len(history) == 1
    assert history[0].id == result.observations[0].id


def test_partial_refresh_preserves_old_state_and_exposes_failure():
    _, controller, _, _, state = _services()
    state.scan("profile", FleetSelection.several(1, 2), source="seed")
    controller.calls.clear()
    controller.fail_at = 2

    result = state.state(
        "profile",
        FleetStateRequest(
            FleetSelection.several(1, 2, 3),
            FleetRefreshPolicy.ALWAYS,
        ),
        source="consumer:test",
    )

    assert controller.calls == [1, 2]
    assert tuple(item.fleet_index for item in result.observations) == (1, 2)
    assert result.missing_fleet_indices == (3,)
    assert result.refresh_result.status is FleetScanRunStatus.PARTIAL


def test_request_validates_policy_and_timezone_aware_clock():
    with pytest.raises(ValueError):
        FleetStateRequest(FleetSelection.one(1), FleetRefreshPolicy.IF_STALE)
    with pytest.raises(ValueError):
        FleetStateRequest(
            FleetSelection.one(1),
            FleetRefreshPolicy.NEVER,
            max_age=timedelta(seconds=1),
        )
    with pytest.raises(ValueError):
        FleetStateRequest(
            FleetSelection.one(1),
            FleetRefreshPolicy.IF_STALE,
            max_age=timedelta(seconds=-1),
        )

    _, _, _, scanner, _ = _services()
    scanner._clock = lambda: datetime(2026, 8, 25)  # noqa: DTZ001
    with pytest.raises(ValueError):
        scanner.scan("profile", FleetSelection.one(1), source="consumer:test")


def test_runtime_identity_contract_remains_shared():
    digest, identity_id = runtime_instance_identity("profile")
    assert len(digest) == 64
    assert isinstance(identity_id, UUID)
