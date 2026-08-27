from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from module.application.fleet_state import FleetStateObservation
from module.application.morale import (
    MoraleContinuityError,
    MoraleKnowledge,
    MoraleObservation,
    MoraleRecoveryProfile,
    MoraleService,
    RecordMoraleObservation,
    project_morale,
)
from module.application.storage_models import InstanceIdentity
from module.dock_inventory.model import CanonicalShipIdentity, IdentityStatus, ShipForm
from module.formation.model import (
    FleetSelection,
    FormationFleetSide,
    FormationFleetSlotObservation,
    FormationFleetSnapshot,
)


def _identity(ship: int) -> CanonicalShipIdentity:
    return CanonicalShipIdentity(f"azur_lane_ship_group:{ship}")


def _slot(
    side: FormationFleetSide,
    position: int,
    *,
    ship: int | None = None,
    status: IdentityStatus = IdentityStatus.MATCHED,
    form: ShipForm = ShipForm.BASE,
) -> FormationFleetSlotObservation:
    if ship is None:
        return FormationFleetSlotObservation(side=side, position=position, occupied=False)
    return FormationFleetSlotObservation(
        side=side,
        position=position,
        occupied=True,
        identity_status=status,
        raw_name_ocr=f"Ship {ship}",
        displayed_name=f"Ship {ship}",
        canonical_identity=_identity(ship) if status is IdentityStatus.MATCHED else None,
        canonical_name=f"Ship {ship}" if status is IdentityStatus.MATCHED else None,
        ship_form=form if status is IdentityStatus.MATCHED else None,
    )


def _formation(
    instance_id: UUID,
    fleet_index: int,
    *,
    main_1: FormationFleetSlotObservation | None = None,
    vanguard_1: FormationFleetSlotObservation | None = None,
    observed_at: datetime | None = None,
) -> FleetStateObservation:
    slots = (
        main_1 or _slot(FormationFleetSide.MAIN, 1, ship=1),
        _slot(FormationFleetSide.MAIN, 2),
        _slot(FormationFleetSide.MAIN, 3),
        vanguard_1 or _slot(FormationFleetSide.VANGUARD, 1),
        _slot(FormationFleetSide.VANGUARD, 2),
        _slot(FormationFleetSide.VANGUARD, 3),
    )
    return FleetStateObservation(
        id=uuid4(),
        run_id=uuid4(),
        instance_id=instance_id,
        idempotency_key=f"fleet:{uuid4()}",
        observed_at=observed_at or datetime(2026, 8, 27, tzinfo=UTC),
        snapshot=FormationFleetSnapshot(
            fleet_index=fleet_index,
            slots=slots,
            catalog_fingerprint="a" * 64,
        ),
    )


def _morale_observation(
    *,
    baseline: Decimal = Decimal(50),
    observed_at: datetime = datetime(2026, 8, 27, tzinfo=UTC),
    recovery: MoraleRecoveryProfile | None = None,
) -> MoraleObservation:
    return MoraleObservation(
        id=uuid4(),
        formation_snapshot_id=uuid4(),
        instance_id=uuid4(),
        fleet_index=1,
        side=FormationFleetSide.MAIN,
        position=1,
        canonical_identity=_identity(1),
        ship_form=ShipForm.BASE,
        baseline=baseline,
        observed_at=observed_at,
        recovery=recovery or MoraleRecoveryProfile.outside_dorm_base(),
        source="test:exact",
        idempotency_key=f"morale:{uuid4()}",
    )


class _Instances:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], InstanceIdentity] = {}

    def resolve(self, *, alias_kind, alias_digest):
        return self.values.get((alias_kind, alias_digest))

    def register(
        self,
        identity,
        *,
        alias_kind,
        alias_digest,
        source_provenance,
    ):
        del source_provenance
        self.values[(alias_kind, alias_digest)] = identity
        return True


class _FleetRepository:
    def __init__(self) -> None:
        self.observations: list[FleetStateObservation] = []
        self.latest_calls = 0

    def latest(self, instance_id, selection):
        self.latest_calls += 1
        latest = {}
        for item in self.observations:
            if item.instance_id != instance_id or item.fleet_index not in selection.fleet_indices:
                continue
            previous = latest.get(item.fleet_index)
            if previous is None or (item.observed_at, item.id) > (
                previous.observed_at,
                previous.id,
            ):
                latest[item.fleet_index] = item
        return tuple(latest[index] for index in sorted(latest))


class _MoraleRepository:
    def __init__(self) -> None:
        self.observations: list[MoraleObservation] = []
        self.latest_calls = 0

    def append(self, observation):
        for existing in self.observations:
            if existing.idempotency_key == observation.idempotency_key:
                comparable = replace(observation, id=existing.id)
                if existing == comparable:
                    return existing
                raise RuntimeError("synthetic idempotency conflict")
        self.observations.append(observation)
        return observation

    def latest(self, instance_id, selection):
        self.latest_calls += 1
        latest = {}
        for item in self.observations:
            key = (item.fleet_index, item.side, item.position)
            if item.instance_id != instance_id or item.fleet_index not in selection.fleet_indices:
                continue
            previous = latest.get(key)
            if previous is None or (item.observed_at, item.id) > (
                previous.observed_at,
                previous.id,
            ):
                latest[key] = item
        return tuple(latest[key] for key in sorted(latest, key=lambda value: (value[0], value[1].value, value[2])))


class _Uow:
    def __init__(self, instances, fleet_state, morale):
        self.instances = instances
        self.fleet_state = fleet_state
        self.morale = morale
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None

    def commit(self):
        self.commits += 1

    def rollback(self):
        return None


def _service(*, clock=None):
    instances = _Instances()
    fleets = _FleetRepository()
    morale = _MoraleRepository()

    def factory():
        return _Uow(instances, fleets, morale)

    effective_clock = clock or (lambda: datetime(2026, 8, 27, 2, tzinfo=UTC))
    return instances, fleets, morale, MoraleService(factory, clock=effective_clock)


def _seed_fleet(
    instances: _Instances,
    fleets: _FleetRepository,
    instance: str,
    fleet_index: int,
    **kwargs,
) -> FleetStateObservation:
    from module.application.instance_identity import runtime_instance_identity

    digest, instance_id = runtime_instance_identity(instance)
    instances.values[("legacy_instance", digest)] = InstanceIdentity(instance_id, instance)
    observation = _formation(instance_id, fleet_index, **kwargs)
    fleets.observations.append(observation)
    return observation


def _command(
    *,
    fleet_index: int = 1,
    side: FormationFleetSide = FormationFleetSide.MAIN,
    position: int = 1,
    ship: int = 1,
    form: ShipForm = ShipForm.BASE,
    baseline: Decimal = Decimal(50),
    observed_at: datetime | None = datetime(2026, 8, 27, tzinfo=UTC),
    idempotency_key: str = "morale:test",
) -> RecordMoraleObservation:
    return RecordMoraleObservation(
        fleet_index=fleet_index,
        side=side,
        position=position,
        canonical_identity=_identity(ship),
        ship_form=form,
        baseline=baseline,
        recovery=MoraleRecoveryProfile.outside_dorm_base(),
        source="test:observation",
        idempotency_key=idempotency_key,
        observed_at=observed_at,
    )


def test_projection_uses_completed_six_minute_ticks_and_base_20_per_hour():
    observation = _morale_observation()
    start = observation.observed_at

    before_tick = project_morale(observation, at=start + timedelta(minutes=5, seconds=59))
    first_tick = project_morale(observation, at=start + timedelta(minutes=6))
    one_hour = project_morale(observation, at=start + timedelta(hours=1))

    assert before_tick.value == Decimal(50)
    assert before_tick.knowledge is MoraleKnowledge.EXACT
    assert first_tick.value == Decimal(52)
    assert first_tick.knowledge is MoraleKnowledge.PROJECTED
    assert one_hour.value == Decimal(70)


def test_projection_has_exact_decimal_arithmetic_and_long_interval_ceiling():
    recovery = MoraleRecoveryProfile(Decimal(1), Decimal(119), "test:one-per-hour")
    observation = _morale_observation(baseline=Decimal("0.1"), recovery=recovery)

    projected = project_morale(observation, at=observation.observed_at + timedelta(minutes=18))
    long = project_morale(observation, at=observation.observed_at + timedelta(days=3650))

    assert projected.value == Decimal("0.4")
    assert long.value == Decimal(119)
    assert isinstance(projected.value, Decimal)


def test_outside_dorm_ceiling_does_not_lower_baseline_above_119():
    observation = _morale_observation(baseline=Decimal(130))

    projected = project_morale(observation, at=observation.observed_at + timedelta(days=10))

    assert projected.value == Decimal(130)
    assert projected.knowledge is MoraleKnowledge.PROJECTED


@pytest.mark.parametrize("baseline", [Decimal(-1), Decimal(151)])
def test_morale_hard_bounds_fail_closed(baseline):
    with pytest.raises(ValueError):
        _morale_observation(baseline=baseline)


def test_projection_rejects_naive_or_retrograde_time_and_float_input():
    observation = _morale_observation()
    with pytest.raises(ValueError):
        project_morale(observation, at=datetime(2026, 8, 26, tzinfo=UTC))
    with pytest.raises(ValueError):
        project_morale(observation, at=datetime(2026, 8, 27))  # noqa: DTZ001
    with pytest.raises(TypeError):
        MoraleRecoveryProfile(20.0, Decimal(119), "test:float")  # type: ignore[arg-type]


def test_record_uses_injected_clock_and_reads_exact_current_slot():
    now = datetime(2026, 8, 27, 10, tzinfo=UTC)
    instances, fleets, morale, service = _service(clock=lambda: now)
    formation = _seed_fleet(instances, fleets, "profile", 1)

    recorded = service.record("profile", _command(observed_at=None))
    state = service.fleet("profile", 1)

    assert recorded.observed_at == now
    assert recorded.formation_snapshot_id == formation.id
    assert morale.observations == [recorded]
    assert state.slots[0].current == Decimal(50)
    assert state.slots[0].knowledge is MoraleKnowledge.EXACT


def test_repeat_record_returns_the_persisted_idempotent_observation():
    now = datetime(2026, 8, 27, 10, tzinfo=UTC)
    instances, fleets, morale, service = _service(clock=lambda: now)
    _seed_fleet(instances, fleets, "profile", 1)
    command = _command(observed_at=now)

    first = service.record("profile", command)
    repeated = service.record("profile", command)

    assert repeated == first
    assert len(morale.observations) == 1


def test_record_rejects_future_observation_against_injected_clock():
    now = datetime(2026, 8, 27, 10, tzinfo=UTC)
    _, _, _, service = _service(clock=lambda: now)

    with pytest.raises(ValueError):
        service.record("profile", _command(observed_at=now + timedelta(seconds=1)))


def test_record_rejects_observation_older_than_proven_fleet_state():
    now = datetime(2026, 8, 27, 10, tzinfo=UTC)
    instances, fleets, _, service = _service(clock=lambda: now)
    _seed_fleet(
        instances,
        fleets,
        "profile",
        1,
        observed_at=now - timedelta(minutes=1),
    )

    with pytest.raises(MoraleContinuityError):
        service.record(
            "profile",
            _command(observed_at=now - timedelta(minutes=2)),
        )


def test_canonical_identity_is_bounded_for_persistence_contract():
    with pytest.raises(ValueError):
        RecordMoraleObservation(
            fleet_index=1,
            side=FormationFleetSide.MAIN,
            position=1,
            canonical_identity=CanonicalShipIdentity("x" * 129),
            ship_form=ShipForm.BASE,
            baseline=Decimal(50),
            recovery=MoraleRecoveryProfile.outside_dorm_base(),
            source="test:observation",
            idempotency_key="bounded",
        )


def test_same_ship_and_form_in_same_slot_preserves_continuity_after_new_scan():
    instances, fleets, _, service = _service()
    first = _seed_fleet(instances, fleets, "profile", 1)
    service.record("profile", _command())
    second = _formation(
        first.instance_id,
        1,
        observed_at=first.observed_at + timedelta(minutes=1),
    )
    fleets.observations.append(second)

    state = service.fleet("profile", 1, at=datetime(2026, 8, 27, 1, tzinfo=UTC))

    assert state.formation_observation_id == second.id
    assert state.slots[0].current == Decimal(70)
    assert state.slots[0].knowledge is MoraleKnowledge.PROJECTED


@pytest.mark.parametrize(
    "replacement",
    [
        _slot(FormationFleetSide.MAIN, 1, ship=2),
        _slot(FormationFleetSide.MAIN, 1, ship=1, form=ShipForm.RETROFIT),
    ],
)
def test_identity_or_form_change_invalidates_old_morale(replacement):
    instances, fleets, _, service = _service()
    first = _seed_fleet(instances, fleets, "profile", 1)
    service.record("profile", _command())
    fleets.observations.append(
        _formation(
            first.instance_id,
            1,
            main_1=replacement,
            observed_at=first.observed_at + timedelta(minutes=1),
        )
    )

    state = service.fleet("profile", 1, at=datetime(2026, 8, 27, 1, tzinfo=UTC))

    assert state.slots[0].knowledge is MoraleKnowledge.UNKNOWN
    assert state.slots[0].current is None


@pytest.mark.parametrize(
    "slot",
    [
        _slot(FormationFleetSide.MAIN, 1),
        _slot(FormationFleetSide.MAIN, 1, ship=1, status=IdentityStatus.UNRESOLVED),
        _slot(FormationFleetSide.MAIN, 1, ship=1, status=IdentityStatus.AMBIGUOUS),
    ],
)
def test_empty_unresolved_and_ambiguous_slots_stay_unknown(slot):
    instances, fleets, _, service = _service()
    _seed_fleet(instances, fleets, "profile", 1, main_1=slot)

    state = service.fleet("profile", 1, at=datetime(2026, 8, 27, tzinfo=UTC))

    assert state.slots[0].knowledge is MoraleKnowledge.UNKNOWN
    assert state.slots[0].current is None
    with pytest.raises(MoraleContinuityError):
        service.record("profile", _command())


def test_identical_canonical_copies_in_slots_and_fleets_do_not_mix():
    instances, fleets, _, service = _service()
    _seed_fleet(
        instances,
        fleets,
        "profile",
        1,
        vanguard_1=_slot(FormationFleetSide.VANGUARD, 1, ship=1),
    )
    _seed_fleet(instances, fleets, "profile", 2)
    service.record("profile", _command(baseline=Decimal(10), idempotency_key="m:1"))
    service.record(
        "profile",
        _command(
            side=FormationFleetSide.VANGUARD,
            baseline=Decimal(20),
            idempotency_key="m:2",
        ),
    )
    service.record(
        "profile",
        _command(fleet_index=2, baseline=Decimal(30), idempotency_key="m:3"),
    )

    state = service.state(
        "profile",
        FleetSelection.several(1, 2),
        at=datetime(2026, 8, 27, tzinfo=UTC),
    )

    assert state.fleets[0].slots[0].current == Decimal(10)
    assert state.fleets[0].slots[3].current == Decimal(20)
    assert state.fleets[1].slots[0].current == Decimal(30)


def test_app_instances_are_fully_isolated():
    instances, fleets, _, service = _service()
    _seed_fleet(instances, fleets, "profile-a", 1)
    _seed_fleet(instances, fleets, "profile-b", 1)
    service.record("profile-a", _command(baseline=Decimal(10), idempotency_key="a"))
    service.record("profile-b", _command(baseline=Decimal(90), idempotency_key="b"))

    a = service.fleet("profile-a", 1, at=datetime(2026, 8, 27, tzinfo=UTC))
    b = service.fleet("profile-b", 1, at=datetime(2026, 8, 27, tzinfo=UTC))

    assert a.slots[0].current == Decimal(10)
    assert b.slots[0].current == Decimal(90)


def test_missing_fleet_returns_six_honest_unknown_slots():
    _, fleets, morale, service = _service()

    state = service.fleet("new-profile", 6, at=datetime(2026, 8, 27, tzinfo=UTC))

    assert state.formation_observation_id is None
    assert len(state.slots) == 6
    assert all(item.occupied is None for item in state.slots)
    assert all(item.knowledge is MoraleKnowledge.UNKNOWN for item in state.slots)
    assert fleets.latest_calls == 1
    assert morale.latest_calls == 1


def test_full_selection_uses_set_based_repository_reads_without_n_plus_one():
    instances, fleets, morale, service = _service()
    for index in range(1, 7):
        _seed_fleet(instances, fleets, "profile", index)
    fleets.latest_calls = 0

    state = service.state(
        "profile",
        FleetSelection.all(),
        at=datetime(2026, 8, 27, tzinfo=UTC),
    )

    assert len(state.fleets) == 6
    assert fleets.latest_calls == 1
    assert morale.latest_calls == 1


def test_record_rejects_identity_or_form_not_proven_by_current_fleet_state():
    instances, fleets, _, service = _service()
    _seed_fleet(instances, fleets, "profile", 1)

    with pytest.raises(MoraleContinuityError):
        service.record("profile", _command(ship=2))
    with pytest.raises(MoraleContinuityError):
        service.record("profile", _command(form=ShipForm.RETROFIT))


def test_newer_observation_wins_deterministically_for_one_physical_slot():
    instances, fleets, morale, service = _service()
    formation = _seed_fleet(instances, fleets, "profile", 1)
    older = service.record("profile", _command(baseline=Decimal(10), idempotency_key="old"))
    newer = replace(
        older,
        id=UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
        formation_snapshot_id=formation.id,
        baseline=Decimal(90),
        idempotency_key="new",
    )
    morale.observations.append(newer)

    state = service.fleet("profile", 1, at=datetime(2026, 8, 27, tzinfo=UTC))

    assert state.slots[0].morale_observation_id == newer.id
    assert state.slots[0].current == Decimal(90)
