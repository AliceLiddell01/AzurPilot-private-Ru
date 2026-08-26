from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest

from module.application.fleet_manual_scan import (
    FleetManualScanCommand,
    FleetManualScanStatus,
)
from module.application.fleet_page import FleetPageQueryService, FleetSlotState
from module.application.fleet_state import FleetStateObservation
from module.application.instance_identity import runtime_instance_identity
from module.application.storage_models import InstanceIdentity
from module.dock_inventory.model import CanonicalShipIdentity, IdentityStatus, ShipForm
from module.formation.model import (
    FormationFleetSide,
    FormationFleetSlotObservation,
    FormationFleetSnapshot,
)
from module.webui.app_fleet_page import (
    FleetPageMixin,
    fleet_slot_text,
    format_fleet_timestamp,
)
from module.webui import lang


def _slot(
    side: FormationFleetSide,
    position: int,
    status: IdentityStatus | None,
    *,
    canonical_name: str | None = None,
    ship_form: ShipForm = ShipForm.BASE,
) -> FormationFleetSlotObservation:
    if status is None:
        return FormationFleetSlotObservation(side=side, position=position, occupied=False)
    matched = status is IdentityStatus.MATCHED
    return FormationFleetSlotObservation(
        side=side,
        position=position,
        occupied=True,
        identity_status=status,
        raw_name_ocr=f"raw-{side.value}-{position}",
        displayed_name=f"display-{side.value}-{position}",
        canonical_identity=(
            CanonicalShipIdentity(f"azur_lane_ship_group:{position}")
            if matched
            else None
        ),
        canonical_name=(canonical_name or f"Ship {position}") if matched else None,
        ship_form=ship_form if matched else None,
    )


def _observation(
    instance_id: UUID,
    fleet_index: int,
    *,
    complete: bool,
    canonical_name: str | None = None,
    first_ship_form: ShipForm = ShipForm.BASE,
) -> FleetStateObservation:
    statuses = (
        IdentityStatus.MATCHED,
        None,
        IdentityStatus.UNRESOLVED if not complete else IdentityStatus.MATCHED,
        IdentityStatus.AMBIGUOUS if not complete else IdentityStatus.MATCHED,
        None,
        None,
    )
    coordinates = (
        (FormationFleetSide.MAIN, 1),
        (FormationFleetSide.MAIN, 2),
        (FormationFleetSide.MAIN, 3),
        (FormationFleetSide.VANGUARD, 1),
        (FormationFleetSide.VANGUARD, 2),
        (FormationFleetSide.VANGUARD, 3),
    )
    run_id = uuid4()
    return FleetStateObservation(
        id=uuid4(),
        run_id=run_id,
        instance_id=instance_id,
        idempotency_key=f"test:{run_id}:{fleet_index}",
        observed_at=datetime(2026, 8, 25, 1, 2, 3, tzinfo=UTC),
        snapshot=FormationFleetSnapshot(
            fleet_index=fleet_index,
            slots=tuple(
                _slot(
                    side,
                    position,
                    status,
                    canonical_name=canonical_name if position == 1 else None,
                    ship_form=(
                        first_ship_form
                        if side is FormationFleetSide.MAIN and position == 1
                        else ShipForm.BASE
                    ),
                )
                for (side, position), status in zip(coordinates, statuses, strict=True)
            ),
            catalog_fingerprint="a" * 64,
        ),
    )


class _Instances:
    def __init__(self) -> None:
        self.values: dict[str, InstanceIdentity] = {}

    def resolve(self, *, alias_kind, alias_digest):
        del alias_kind
        return self.values.get(alias_digest)

    def register(
        self,
        identity,
        *,
        alias_kind,
        alias_digest,
        source_provenance,
    ):
        del alias_kind, source_provenance
        self.values[alias_digest] = identity
        return True


class _FleetStateRepository:
    def __init__(self) -> None:
        self.by_instance: dict[UUID, tuple[FleetStateObservation, ...]] = {}
        self.calls = []

    def latest(self, instance_id, selection):
        self.calls.append((instance_id, selection.fleet_indices))
        return self.by_instance.get(instance_id, ())


class _CommandRepository:
    def __init__(self) -> None:
        self.by_instance = {}
        self.calls = []

    def latest(self, instance_id):
        self.calls.append(instance_id)
        return self.by_instance.get(instance_id)


class _Uow:
    def __init__(self, instances, fleet_state, commands):
        self.instances = instances
        self.fleet_state = fleet_state
        self.fleet_scan_commands = commands
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def commit(self):
        self.commits += 1


def _service():
    instances = _Instances()
    state = _FleetStateRepository()
    commands = _CommandRepository()
    uow = _Uow(instances, state, commands)
    return FleetPageQueryService(lambda: uow), state, commands, uow


def test_page_always_has_six_rows_and_no_data_state() -> None:
    service, state, commands, uow = _service()

    model = service.view("profile-a")

    assert tuple(row.fleet_index for row in model.rows) == (1, 2, 3, 4, 5, 6)
    assert all(row.observed_at is None and row.complete is None for row in model.rows)
    assert len(state.calls) == 1
    assert state.calls[0][1] == (1, 2, 3, 4, 5, 6)
    assert len(commands.calls) == 1
    assert uow.commits == 1


def test_page_projects_complete_incomplete_and_all_slot_states_in_order() -> None:
    service, state, _, _ = _service()
    _, instance_id = runtime_instance_identity("profile-a")
    long_name = "Very Long Canonical Ship Name " * 12
    state.by_instance[instance_id] = (
        _observation(instance_id, 1, complete=True),
        _observation(
            instance_id,
            2,
            complete=False,
            canonical_name=long_name,
        ),
    )

    model = service.view("profile-a")

    assert model.rows[0].complete is True
    assert model.rows[1].complete is False
    assert tuple(slot.state for slot in model.rows[1].slots) == (
        FleetSlotState.MATCHED,
        FleetSlotState.EMPTY,
        FleetSlotState.UNRESOLVED,
        FleetSlotState.AMBIGUOUS,
        FleetSlotState.EMPTY,
        FleetSlotState.EMPTY,
    )
    assert tuple((slot.side.value, slot.position) for slot in model.rows[1].slots) == (
        ("main", 1),
        ("main", 2),
        ("main", 3),
        ("vanguard", 1),
        ("vanguard", 2),
        ("vanguard", 3),
    )
    assert model.rows[1].slots[0].canonical_name == long_name
    assert model.rows[1].slots[0].canonical_identity == "azur_lane_ship_group:1"
    assert model.rows[1].slots[0].ship_form is ShipForm.BASE


def test_retrofit_display_preserves_group_identity_and_base_canonical_name() -> None:
    service, state, _, _ = _service()
    _, instance_id = runtime_instance_identity("profile-a")
    state.by_instance[instance_id] = (
        _observation(
            instance_id,
            1,
            complete=True,
            canonical_name="Generic Test Ship",
            first_ship_form=ShipForm.RETROFIT,
        ),
    )

    slot = service.view("profile-a").rows[0].slots[0]

    assert slot.canonical_identity == "azur_lane_ship_group:1"
    assert slot.canonical_name == "Generic Test Ship"
    assert slot.ship_form is ShipForm.RETROFIT
    assert slot.canonical_display_name == "Generic Test Ship (Retrofit)"
    assert fleet_slot_text(slot) == "Generic Test Ship (Retrofit)"


def test_page_isolates_instances_and_failed_command_preserves_observation() -> None:
    service, state, commands, _ = _service()
    _, instance_a = runtime_instance_identity("profile-a")
    _, instance_b = runtime_instance_identity("profile-b")
    state.by_instance[instance_a] = (_observation(instance_a, 1, complete=True),)
    state.by_instance[instance_b] = (_observation(instance_b, 2, complete=False),)
    commands.by_instance[instance_a] = FleetManualScanCommand(
        id=uuid4(),
        instance_id=instance_a,
        selection=__import__(
            "module.formation.model", fromlist=["FleetSelection"]
        ).FleetSelection.one(2),
        created_at=datetime(2026, 8, 25, tzinfo=UTC),
        started_at=datetime(2026, 8, 25, tzinfo=UTC),
        finished_at=datetime(2026, 8, 25, tzinfo=UTC) + timedelta(seconds=1),
        status=FleetManualScanStatus.FAILED,
        error_code="physical_scan_failed",
    )

    model_a = service.view("profile-a")
    model_b = service.view("profile-b")

    assert model_a.rows[0].observed_at is not None
    assert model_a.manual_command.status is FleetManualScanStatus.FAILED
    assert model_b.rows[0].observed_at is None
    assert model_b.rows[1].observed_at is not None


def test_timestamp_and_slot_text_contracts(monkeypatch) -> None:
    lang.reload()
    assert format_fleet_timestamp(
        datetime(2026, 8, 25, 1, 2, 3, tzinfo=UTC),
        ZoneInfo("Asia/Novosibirsk"),
    ) == "25.08.2026 08:02:03 +07"
    with pytest.raises(ValueError):
        format_fleet_timestamp(
            datetime(2026, 8, 25, 1, 2, 3),
            ZoneInfo("UTC"),
        )

    service, state, _, _ = _service()
    _, instance_id = runtime_instance_identity("profile-a")
    state.by_instance[instance_id] = (_observation(instance_id, 1, complete=False),)
    slots = service.view("profile-a").rows[0].slots
    assert fleet_slot_text(slots[0]) == "Ship 1"
    assert fleet_slot_text(slots[1]) == "Пусто"
    assert fleet_slot_text(slots[2]) == "Не распознано: display-main-3"
    assert fleet_slot_text(slots[3]) == "Неоднозначно: display-vanguard-1"


def test_fleet_page_i18n_has_ru_en_key_and_placeholder_parity() -> None:
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    ru = json.loads((root / "module/config/i18n/ru-RU.json").read_text("utf-8"))
    en = json.loads((root / "module/config/i18n/en-US.json").read_text("utf-8"))
    assert ru["Gui"]["FleetPage"].keys() == en["Gui"]["FleetPage"].keys()
    assert ru["Gui"]["FleetPage"]["Title"] == "Флоты"
    assert "{state}" in ru["Gui"]["FleetPage"]["CommandStatus"]
    assert "{state}" in en["Gui"]["FleetPage"]["CommandStatus"]


def test_fleet_page_uses_event_shop_workspace_and_responsive_stack() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    page_source = (root / "module/webui/app_fleet_page.py").read_text("utf-8")
    stylesheet = (root / "assets/gui/css/fleet-page-alas.css").read_text("utf-8")

    assert 'size="minmax(0, 1fr) minmax(330px, 360px)"' in page_source
    assert '.style("--fleet-scheduler-card--")' in page_source
    assert "position: sticky;" in stylesheet
    assert "grid-auto-flow: row !important;" in stylesheet
    assert "grid-template-columns: minmax(0, 1fr) !important;" in stylesheet
