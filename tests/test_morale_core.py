from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from module.application.fleet_state import FleetStateObservation
from module.application.morale import (
    MoraleContinuityError,
    MoraleEventKind,
    MoraleKnowledge,
    MoraleLocation,
    MoraleObservation,
    MoraleRecoveryProfile,
    MoraleService,
    RecordMoraleEvent,
    RecordMoraleObservation,
    project_morale,
)
from module.application.storage_models import InstanceIdentity
from module.campaign.gems_farming import GemsCampaignOverride, GemsEmotion
from module.combat.emotion import Emotion
from module.dock_inventory.model import CanonicalShipIdentity, IdentityStatus, ShipForm
from module.exception import CampaignEnd, ScriptEnd
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

    def contains_idempotency(self, instance_id, keys):
        return frozenset(
            key
            for key in keys
            if any(
                observation.instance_id == instance_id
                and observation.idempotency_key == key
                for observation in self.observations
            )
        )


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


def _full_formation(instance_id: UUID, fleet_index: int = 1) -> FleetStateObservation:
    base = _formation(instance_id, fleet_index)
    coordinates = (
        (FormationFleetSide.MAIN, 1),
        (FormationFleetSide.MAIN, 2),
        (FormationFleetSide.MAIN, 3),
        (FormationFleetSide.VANGUARD, 1),
        (FormationFleetSide.VANGUARD, 2),
        (FormationFleetSide.VANGUARD, 3),
    )
    snapshot = replace(
        base.snapshot,
        slots=tuple(
            _slot(side, position, ship=index)
            for index, (side, position) in enumerate(coordinates, start=1)
        ),
    )
    return replace(base, snapshot=snapshot)


def _seed_full_fleet(
    instances: _Instances,
    fleets: _FleetRepository,
    instance: str,
    fleet_index: int = 1,
) -> FleetStateObservation:
    from module.application.instance_identity import runtime_instance_identity

    digest, instance_id = runtime_instance_identity(instance)
    instances.values[("legacy_instance", digest)] = InstanceIdentity(instance_id, instance)
    observation = _full_formation(instance_id, fleet_index)
    fleets.observations.append(observation)
    return observation


def _event(
    *,
    kind: MoraleEventKind,
    cost: Decimal,
    key: str,
    observed_at: datetime,
    target: tuple[FormationFleetSide, int] | None = None,
) -> RecordMoraleEvent:
    return RecordMoraleEvent(
        fleet_index=1,
        kind=kind,
        cost=cost,
        source=f"test:{kind.value}",
        event_key=key,
        observed_at=observed_at,
        target_side=target[0] if target is not None else None,
        target_position=target[1] if target is not None else None,
    )


def _emotion_config(*, mode="calculate", control="prevent_red_face", run="run"):
    return SimpleNamespace(
        config_name="profile",
        Campaign_Name="test-campaign",
        Campaign_Use2xBook=False,
        Emotion_Mode=mode,
        Emotion_Fleet1Control=control,
        Emotion_Fleet2Control=control,
        Fleet_Fleet1=1,
        Fleet_Fleet2=2,
        Fleet_FleetOrder="fleet1_all_fleet2_standby",
        Scheduler_NextRun=run,
        task=SimpleNamespace(command="Main"),
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


def test_emotion_event_identity_is_stable_across_restart_and_changes_per_run():
    config = SimpleNamespace(
        Emotion_Fleet1Control="prevent_green_face",
        Emotion_Fleet2Control="prevent_green_face",
        Scheduler_NextRun=datetime(2026, 8, 27, tzinfo=UTC),
        task=SimpleNamespace(command="Main"),
    )
    first = Emotion(config)
    second = Emotion(config)

    first.begin_event("coalition-scuttle:unknown:0")
    retry_key = first._active_event_key
    first.begin_event("coalition-scuttle:unknown:0")

    assert first._active_event_key == retry_key
    assert second._active_event_key is None
    second.begin_event("coalition-scuttle:unknown:0")
    assert second._active_event_key == retry_key
    config.Scheduler_NextRun = datetime(2026, 8, 27, 1, tzinfo=UTC)
    third = Emotion(config)
    third.begin_event("coalition-scuttle:unknown:0")
    assert third._active_event_key != retry_key


def test_emotion_execution_id_separates_same_coordinate_within_scheduler_run():
    config = _emotion_config(run="run-a")
    emotion = Emotion(config)

    emotion.begin_event(
        "combat:campaign:0:1",
        execution_id="combat:campaign:0:1:attempt-1",
    )
    first = emotion._active_event_key
    emotion.begin_event(
        "combat:campaign:0:1",
        execution_id="combat:campaign:0:1:attempt-2",
    )

    assert emotion._active_event_key != first


def test_bug_threshold_reset_is_safe_before_cached_property_evaluation():
    emotion = object.__new__(Emotion)

    emotion.bug_threshold_reset()

    assert "bug_threshold" not in emotion.__dict__


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


def test_battle_event_is_per_slot_and_idempotent_on_retry():
    now = datetime(2026, 8, 27, 10, tzinfo=UTC)
    event_at = now + timedelta(minutes=1)
    instances, fleets, morale, service = _service(clock=lambda: event_at)
    _seed_fleet(instances, fleets, "profile", 1, observed_at=now)
    service.record("profile", _command(observed_at=now, baseline=Decimal(50)))
    command = RecordMoraleEvent(
        fleet_index=1,
        kind=MoraleEventKind.BATTLE,
        cost=Decimal(2),
        source="test:battle",
        event_key="battle:one",
        observed_at=event_at,
    )

    first = service.apply_event("profile", command)
    repeated = service.apply_event("profile", command)

    assert first.applied and first.applied_slots == 1
    assert first.exact_slots == 1 and first.unknown_slots == 0
    assert repeated.applied is False
    assert repeated.skipped_slots == 1
    assert len(morale.observations) == 2
    assert service.fleet("profile", 1, at=event_at).slots[0].current == Decimal(48)


@pytest.mark.parametrize(
    ("location", "recovery"),
    [
        (
            MoraleLocation.DORM_FLOOR_1,
            MoraleRecoveryProfile(Decimal(40), Decimal(150), "dorm:floor-1"),
        ),
        (
            MoraleLocation.DORM_FLOOR_2,
            MoraleRecoveryProfile(Decimal(50), Decimal(150), "dorm:floor-2"),
        ),
        (MoraleLocation.OUTSIDE_DORM, MoraleRecoveryProfile.outside_dorm_base()),
    ],
)
def test_cost_event_preserves_proven_recovery_context(
    location, recovery
):
    now = datetime(2026, 8, 27, 10, tzinfo=UTC)
    event_at = now + timedelta(minutes=1)
    instances, fleets, morale, service = _service(clock=lambda: event_at)
    _seed_fleet(instances, fleets, "profile", 1, observed_at=now)
    recorded = service.record("profile", _command(observed_at=now, baseline=Decimal(50)))
    dorm_scan_id = uuid4()
    morale.observations[0] = replace(
        recorded,
        recovery=recovery,
        location=location,
        dorm_scan_id=dorm_scan_id,
    )

    result = service.apply_event(
        "profile",
        _event(
            kind=MoraleEventKind.BATTLE,
            cost=Decimal(2),
            key="battle:context",
            observed_at=event_at,
        ),
    )
    slot = service.fleet("profile", 1, at=event_at).slots[0]

    assert result.exact_slots == 1
    assert slot.current == Decimal(48)
    assert slot.recovery == recovery
    assert slot.location is location
    assert slot.dorm_scan_id == dorm_scan_id


def test_warning_invalidates_morale_but_preserves_proven_recovery_context():
    now = datetime(2026, 8, 27, 10, tzinfo=UTC)
    instances, fleets, morale, service = _service(clock=lambda: now)
    _seed_fleet(
        instances,
        fleets,
        "profile",
        1,
        observed_at=now - timedelta(minutes=2),
    )
    recorded = service.record(
        "profile",
        _command(observed_at=now - timedelta(minutes=1), baseline=Decimal(50)),
    )
    recovery = MoraleRecoveryProfile(Decimal(50), Decimal(150), "dorm:floor-2")
    dorm_scan_id = uuid4()
    morale.observations[0] = replace(
        recorded,
        recovery=recovery,
        location=MoraleLocation.DORM_FLOOR_2,
        dorm_scan_id=dorm_scan_id,
    )

    service.record_warning("profile", fleet_index=1, event_key="warning:context")
    slot = service.fleet("profile", 1, at=now).slots[0]

    assert slot.knowledge is MoraleKnowledge.UNKNOWN
    assert slot.current is None
    assert slot.recovery == recovery
    assert slot.location is MoraleLocation.DORM_FLOOR_2
    assert slot.dorm_scan_id == dorm_scan_id


def test_cost_event_does_not_carry_context_across_identity_change():
    now = datetime(2026, 8, 27, 10, tzinfo=UTC)
    event_at = now + timedelta(minutes=1)
    instances, fleets, _, service = _service(clock=lambda: event_at)
    first = _seed_fleet(instances, fleets, "profile", 1, observed_at=now)
    service.record("profile", _command(observed_at=now, baseline=Decimal(50)))
    fleets.observations.append(
        _formation(
            first.instance_id,
            1,
            main_1=_slot(FormationFleetSide.MAIN, 1, ship=2),
            observed_at=event_at,
        )
    )

    service.apply_event(
        "profile",
        _event(
            kind=MoraleEventKind.BATTLE,
            cost=Decimal(2),
            key="battle:replacement",
            observed_at=event_at,
        ),
    )
    slot = service.fleet("profile", 1, at=event_at).slots[0]

    assert slot.knowledge is MoraleKnowledge.UNKNOWN
    assert slot.recovery == MoraleRecoveryProfile.outside_dorm_base()
    assert slot.location is MoraleLocation.UNKNOWN
    assert slot.dorm_scan_id is None


def test_targeted_shipwreck_cost_only_updates_proven_casualty_slot():
    now = datetime(2026, 8, 27, 10, tzinfo=UTC)
    event_at = now + timedelta(minutes=1)
    instances, fleets, morale, service = _service(clock=lambda: event_at)
    formation = _seed_full_fleet(instances, fleets, "profile")
    for index, (side, position) in enumerate(
        (
            (FormationFleetSide.MAIN, 1),
            (FormationFleetSide.MAIN, 2),
            (FormationFleetSide.MAIN, 3),
            (FormationFleetSide.VANGUARD, 1),
            (FormationFleetSide.VANGUARD, 2),
            (FormationFleetSide.VANGUARD, 3),
        ),
        start=1,
    ):
        service.record(
            "profile",
            _command(
                side=side,
                position=position,
                ship=index,
                baseline=Decimal(50),
                observed_at=now,
                idempotency_key=f"morale:{index}",
            ),
        )
    target = (FormationFleetSide.MAIN, 1)
    first = service.apply_event(
        "profile",
        _event(
            kind=MoraleEventKind.SHIPWRECK,
            cost=Decimal(10),
            key="shipwreck:known",
            observed_at=event_at,
            target=target,
        ),
    )
    repeated = service.apply_event(
        "profile",
        _event(
            kind=MoraleEventKind.SHIPWRECK,
            cost=Decimal(10),
            key="shipwreck:known",
            observed_at=event_at,
            target=target,
        ),
    )
    state = service.fleet("profile", 1, at=event_at)

    assert first.applied_slots == 1
    assert first.exact_slots == 1
    assert repeated.applied is False
    assert repeated.skipped_slots == 1
    assert state.slots[0].current == Decimal(40)
    assert all(slot.current == Decimal(50) for slot in state.slots[1:])
    assert len(morale.observations) == 7
    assert formation.snapshot.slots[0].canonical_identity == _identity(1)


def test_unknown_shipwreck_invalidates_without_fleet_wide_exact_cost():
    now = datetime(2026, 8, 27, 10, tzinfo=UTC)
    event_at = now + timedelta(minutes=1)
    instances, fleets, _, service = _service(clock=lambda: event_at)
    _seed_full_fleet(instances, fleets, "profile")
    for index, (side, position) in enumerate(
        (
            (FormationFleetSide.MAIN, 1),
            (FormationFleetSide.MAIN, 2),
            (FormationFleetSide.MAIN, 3),
            (FormationFleetSide.VANGUARD, 1),
            (FormationFleetSide.VANGUARD, 2),
            (FormationFleetSide.VANGUARD, 3),
        ),
        start=1,
    ):
        service.record(
            "profile",
            _command(
                side=side,
                position=position,
                ship=index,
                observed_at=now,
                idempotency_key=f"morale:{index}",
            ),
        )

    result = service.apply_event(
        "profile",
        _event(
            kind=MoraleEventKind.SHIPWRECK,
            cost=Decimal(10),
            key="shipwreck:unknown",
            observed_at=event_at,
        ),
    )
    state = service.fleet("profile", 1, at=event_at)

    assert result.exact_slots == 0
    assert result.unknown_slots == 6
    assert all(slot.knowledge is MoraleKnowledge.UNKNOWN for slot in state.slots)
    assert all(slot.current is None for slot in state.slots)


def test_calculated_readiness_blocks_unknown_slot_without_scheduling_fake_eta():
    now = datetime(2026, 8, 27, 10, tzinfo=UTC)
    instances, fleets, _, service = _service(clock=lambda: now)
    _seed_fleet(instances, fleets, "profile", 1, observed_at=now)
    config = _emotion_config()
    delays = []
    config.task_delay = lambda **kwargs: delays.append(kwargs)

    with pytest.raises(ScriptEnd):
        Emotion(config, morale_service=service).check_reduce(1)

    assert delays == []


def test_check_reduce_rejects_unproven_zero_battle_coordinate_fail_closed():
    config = _emotion_config()

    with pytest.raises(ScriptEnd, match="Число боёв на карте не доказано"):
        Emotion(config).check_reduce(0)


def test_calculated_readiness_blocks_mixed_exact_and_unknown_slots():
    now = datetime(2026, 8, 27, 10, tzinfo=UTC)
    instances, fleets, _, service = _service(clock=lambda: now)
    _seed_fleet(
        instances,
        fleets,
        "profile",
        1,
        main_1=_slot(FormationFleetSide.MAIN, 1, ship=1),
        vanguard_1=_slot(FormationFleetSide.VANGUARD, 1, ship=2),
        observed_at=now,
    )
    service.record(
        "profile",
        _command(observed_at=now, baseline=Decimal(80), idempotency_key="morale:main"),
    )

    recovered, delay = Emotion(
        _emotion_config(), morale_service=service
    )._check_reduce(1)

    assert recovered is None
    assert delay is True


def test_outside_unknown_morale_does_not_invent_recovery_eta():
    now = datetime(2026, 8, 27, 10, tzinfo=UTC)
    instances, fleets, _, service = _service(clock=lambda: now)
    _seed_fleet(instances, fleets, "profile", 1, observed_at=now)

    recovered, delay = Emotion(
        _emotion_config(), morale_service=service
    )._check_reduce(1)

    assert recovered is None
    assert delay is True


def test_readiness_blocks_occupied_slot_with_unresolved_identity():
    now = datetime(2026, 8, 27, 10, tzinfo=UTC)
    instances, fleets, _, service = _service(clock=lambda: now)
    _seed_fleet(
        instances,
        fleets,
        "profile",
        1,
        main_1=_slot(
            FormationFleetSide.MAIN,
            1,
            ship=1,
            status=IdentityStatus.UNRESOLVED,
        ),
        observed_at=now,
    )

    recovered, delay = Emotion(
        _emotion_config(), morale_service=service
    )._check_reduce(1)

    assert recovered is None
    assert delay is True


def test_keep_exp_target_is_not_clamped_to_outside_ceiling():
    config = _emotion_config(control="keep_exp_bonus")
    policy = Emotion(config).fleet_1

    assert Emotion._target(policy, 2, Decimal(119)) == Decimal(122)
    assert Emotion._target(policy, 30, Decimal(119)) == Decimal(149)


def test_keep_exp_unreachable_outside_target_cleanly_blocks_readiness():
    now = datetime(2026, 8, 27, 10, tzinfo=UTC)
    instances, fleets, _, service = _service(clock=lambda: now)
    _seed_fleet(instances, fleets, "profile", 1, observed_at=now)
    service.record("profile", _command(observed_at=now, baseline=Decimal(100)))

    recovered, delay = Emotion(
        _emotion_config(control="keep_exp_bonus"), morale_service=service
    )._check_reduce(1)

    assert recovered is None
    assert delay is True


def test_dorm_ceiling_allows_reachable_keep_exp_target():
    now = datetime(2026, 8, 27, 10, tzinfo=UTC)
    instances, fleets, morale, service = _service(clock=lambda: now)
    _seed_fleet(instances, fleets, "profile", 1, observed_at=now)
    recorded = service.record("profile", _command(observed_at=now, baseline=Decimal(100)))
    morale.observations[0] = replace(
        recorded,
        recovery=MoraleRecoveryProfile(Decimal(50), Decimal(150), "dorm:floor-2"),
        location=MoraleLocation.DORM_FLOOR_2,
        dorm_scan_id=uuid4(),
    )

    recovered, delay = Emotion(
        _emotion_config(control="keep_exp_bonus"), morale_service=service
    )._check_reduce(1)

    assert recovered is not None
    assert recovered > now
    assert delay is True


def test_mixed_recovery_context_blocks_on_one_unreachable_slot():
    now = datetime(2026, 8, 27, 10, tzinfo=UTC)
    instances, fleets, morale, service = _service(clock=lambda: now)
    _seed_fleet(
        instances,
        fleets,
        "profile",
        1,
        main_1=_slot(FormationFleetSide.MAIN, 1, ship=1),
        vanguard_1=_slot(FormationFleetSide.VANGUARD, 1, ship=2),
        observed_at=now,
    )
    first = service.record(
        "profile",
        _command(observed_at=now, baseline=Decimal(100), idempotency_key="morale:main"),
    )
    second = service.record(
        "profile",
        _command(
            side=FormationFleetSide.VANGUARD,
            ship=2,
            observed_at=now,
            baseline=Decimal(100),
            idempotency_key="morale:vanguard",
        ),
    )
    morale.observations[0] = replace(
        first,
        recovery=MoraleRecoveryProfile(Decimal(50), Decimal(150), "dorm:floor-2"),
        location=MoraleLocation.DORM_FLOOR_2,
        dorm_scan_id=uuid4(),
    )
    morale.observations[1] = replace(
        second,
        recovery=MoraleRecoveryProfile.outside_dorm_base(),
        location=MoraleLocation.OUTSIDE_DORM,
        dorm_scan_id=uuid4(),
    )

    recovered, delay = Emotion(
        _emotion_config(control="keep_exp_bonus"), morale_service=service
    )._check_reduce(1)

    assert recovered is None
    assert delay is True


def test_ignore_mode_does_not_turn_unknown_into_a_calculated_ready_signal():
    now = datetime(2026, 8, 27, 10, tzinfo=UTC)
    instances, fleets, _, service = _service(clock=lambda: now)
    _seed_fleet(instances, fleets, "profile", 1, observed_at=now)
    config = _emotion_config(mode="ignore")
    Emotion(config, morale_service=service).check_reduce(1)


def test_wait_blocks_unknown_morale_with_clean_scheduler_boundary():
    now = datetime(2026, 8, 27, 10, tzinfo=UTC)
    instances, fleets, _, service = _service(clock=lambda: now)
    _seed_fleet(instances, fleets, "profile", 1, observed_at=now)

    with pytest.raises(ScriptEnd):
        Emotion(_emotion_config(), morale_service=service).wait(1)


def test_gems_override_stops_on_unknown_instead_of_entering_battle():
    now = datetime(2026, 8, 27, 10, tzinfo=UTC)
    instances, fleets, _, service = _service(clock=lambda: now)
    _seed_fleet(instances, fleets, "profile", 1, observed_at=now)
    config = _emotion_config()
    config.GEMS_EMOTION_TRIGGERED = False

    with pytest.raises(CampaignEnd):
        GemsEmotion(config, morale_service=service).check_reduce(1)

    assert config.GEMS_EMOTION_TRIGGERED is True


def test_gems_ignore_warning_does_not_stop_when_popup_is_not_on_current_frame():
    config = SimpleNamespace(
        GemsFarming_IgnoreEmotionWarning=True,
        GemsFarming_ChangeVanguard="enabled",
        GEMS_EMOTION_TRIGGERED=False,
    )
    campaign = GemsCampaignOverride.__new__(GemsCampaignOverride)
    campaign.config = config
    calls = []

    def handle_warning(**kwargs):
        calls.append(kwargs)
        return False

    campaign._handle_low_morale_warning = handle_warning

    assert campaign.handle_combat_low_emotion() is False
    assert calls == [{"allow_confirm": True, "stop": False}]


def test_warning_invalidates_exact_morale_without_inventing_a_value():
    now = datetime(2026, 8, 27, 10, tzinfo=UTC)
    instances, fleets, morale, service = _service(clock=lambda: now)
    _seed_fleet(
        instances,
        fleets,
        "profile",
        1,
        observed_at=now - timedelta(minutes=2),
    )
    service.record(
        "profile",
        _command(observed_at=now - timedelta(minutes=1), baseline=Decimal(50)),
    )

    result = service.record_warning(
        "profile",
        fleet_index=1,
        event_key="warning:one",
        observed_at=now,
    )
    state = service.fleet("profile", 1, at=now)

    assert result.applied and result.unknown_slots == 1
    assert len(morale.observations) == 2
    assert state.slots[0].knowledge is MoraleKnowledge.UNKNOWN
    assert state.slots[0].current is None
    assert state.slots[0].recovery == MoraleRecoveryProfile.outside_dorm_base()


def test_combat_emotion_facade_uses_logical_to_physical_mapping_and_no_double_write():
    now = datetime(2026, 8, 27, 10, tzinfo=UTC)
    event_at = now + timedelta(minutes=1)
    instances, fleets, morale, service = _service(clock=lambda: event_at)
    _seed_fleet(instances, fleets, "profile", 3, observed_at=now)
    service.record(
        "profile",
        _command(fleet_index=3, observed_at=now, baseline=Decimal(50)),
    )
    config = SimpleNamespace(
        config_name="profile",
        Campaign_Name="test-campaign",
        Campaign_Use2xBook=False,
        Emotion_Mode="calculate_ignore",
        Emotion_Fleet1Control="prevent_red_face",
        Emotion_Fleet2Control="prevent_red_face",
        Fleet_Fleet1=3,
        Fleet_Fleet2=4,
        Fleet_FleetOrder="fleet1_all_fleet2_standby",
        Scheduler_NextRun="test-run",
        task=SimpleNamespace(command="Main"),
    )
    emotion = Emotion(config, morale_service=service)
    emotion.begin_event("combat:test")

    first = emotion.reduce(1)
    repeated = emotion.reduce(1)

    assert first.applied and first.fleet_index == 3
    assert repeated.applied is False
    assert emotion.total_reduced == 2
    assert service.fleet("profile", 3, at=event_at).slots[0].current == Decimal(48)
    assert len(morale.observations) == 2


def test_combat_event_key_is_scoped_to_scheduler_run():
    config = SimpleNamespace(
        Scheduler_NextRun="run-a",
        Emotion_Fleet1Control="prevent_red_face",
        Emotion_Fleet2Control="prevent_red_face",
    )
    emotion = Emotion(config)

    emotion.begin_event("combat:campaign:0:1")
    first = emotion._event_key(1, MoraleEventKind.BATTLE)
    config.Scheduler_NextRun = "run-b"
    emotion.begin_event("combat:campaign:0:1")
    second = emotion._event_key(1, MoraleEventKind.BATTLE)

    assert first != second
    assert len(first) <= 96
    assert len(second) <= 96


def test_active_event_keys_include_logical_fleet_and_event_kind():
    config = _emotion_config(run="run-a")
    emotion = Emotion(config)
    emotion.begin_event("combat:campaign:0:1")

    fleet_one = emotion._event_key(1, MoraleEventKind.BATTLE)
    fleet_two = emotion._event_key(2, MoraleEventKind.BATTLE)
    shipwreck = emotion._event_key(1, MoraleEventKind.SHIPWRECK)

    assert len(fleet_one) <= 96
    assert len(fleet_two) <= 96
    assert len(shipwreck) <= 96
    assert len({fleet_one, fleet_two, shipwreck}) == 3


def test_fallback_event_key_uses_explicit_battle_coordinate():
    config = _emotion_config(run="run-a")
    emotion = Emotion(config)

    first = emotion._event_key(1, MoraleEventKind.BATTLE, battle=0)
    second = emotion._event_key(1, MoraleEventKind.BATTLE, battle=1)

    assert first != second
    assert len(first) <= 96
    assert len(second) <= 96


def test_fallback_reduce_uses_explicit_battle_coordinate_without_begin_event():
    now = datetime(2026, 8, 27, 10, tzinfo=UTC)
    event_at = now + timedelta(minutes=1)
    clock_values = iter((now, now + timedelta(seconds=1), event_at))
    instances, fleets, morale, service = _service(clock=lambda: next(clock_values))
    _seed_fleet(instances, fleets, "profile", 1, observed_at=now)
    service.record("profile", _command(observed_at=now, baseline=Decimal(50)))
    emotion = Emotion(_emotion_config(run="run-a"), morale_service=service)

    first = emotion.reduce(1, battle=0)
    second = emotion.reduce(1, battle=1)

    assert first.applied is True
    assert second.applied is True
    assert len(morale.observations) == 3
    assert service.fleet("profile", 1, at=event_at).slots[0].current == Decimal(46)


def test_new_emotion_object_retries_same_durable_event_without_double_deduction():
    now = datetime(2026, 8, 27, 10, tzinfo=UTC)
    event_at = now + timedelta(minutes=1)
    instances, fleets, morale, service = _service(clock=lambda: event_at)
    _seed_fleet(instances, fleets, "profile", 1, observed_at=now)
    service.record("profile", _command(observed_at=now, baseline=Decimal(50)))
    config = _emotion_config(run="run-a")

    first = Emotion(config, morale_service=service)
    first.begin_event("combat:campaign:0:1")
    applied = first.reduce(1)
    second = Emotion(config, morale_service=service)
    second.begin_event("combat:campaign:0:1")
    repeated = second.reduce(1)

    assert applied.applied is True
    assert repeated.applied is False
    assert service.fleet("profile", 1, at=event_at).slots[0].current == Decimal(48)
    assert len(morale.observations) == 2


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
