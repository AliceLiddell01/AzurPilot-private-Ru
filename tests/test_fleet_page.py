from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
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
from module.dock_inventory.model import CanonicalShipIdentity, IdentityStatus
from module.formation.model import (
    FormationFleetSide,
    FormationFleetSlotObservation,
    FormationFleetSnapshot,
)
from module.webui.app_fleet_page import (
    FleetPageMixin,
    fleet_slot_text,
    format_fleet_timestamp,
    load_fleet_autoscan_config,
    normalize_fleet_autoscan_update,
)
from module.webui import lang


def _slot(
    side: FormationFleetSide,
    position: int,
    status: IdentityStatus | None,
    *,
    canonical_name: str | None = None,
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
    )


def _observation(
    instance_id: UUID,
    fleet_index: int,
    *,
    complete: bool,
    canonical_name: str | None = None,
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


def test_autoscan_backend_reuses_stage2_contract_and_rejects_invalid_values() -> None:
    loaded = load_fleet_autoscan_config(
        {"Alas": {"FleetAutoScan": {"Mode": "daily", "Fleets": [6, 2, 6]}}}
    )
    assert loaded.mode.value == "daily"
    assert loaded.selection.fleet_indices == (2, 6)
    assert normalize_fleet_autoscan_update("every_start", [1, 3]) == {
        "Alas.FleetAutoScan.Mode": "every_start",
        "Alas.FleetAutoScan.Fleets": [1, 3],
    }
    assert normalize_fleet_autoscan_update("disabled", [1, 2, 3, 4, 5, 6])[
        "Alas.FleetAutoScan.Fleets"
    ] == [1, 2, 3, 4, 5, 6]
    with pytest.raises(ValueError):
        normalize_fleet_autoscan_update("sometimes", [1])
    with pytest.raises(ValueError):
        normalize_fleet_autoscan_update("daily", [])
    with pytest.raises(ValueError):
        normalize_fleet_autoscan_update("daily", [7])


def test_autoscan_save_uses_existing_config_pipeline_and_rereads_persisted_value(
    monkeypatch,
) -> None:
    from module.config.deep import deep_set

    import module.webui.app_fleet_page as fleet_page_module

    class ConfigUpdater:
        def __init__(self) -> None:
            self.data = {
                "Alas": {
                    "FleetAutoScan": {
                        "Mode": "disabled",
                        "Fleets": [1, 2, 3, 4, 5, 6],
                    }
                }
            }
            self.load_calls = 0

        def read_file(self, _name):
            return self.data

        def load(self):
            self.load_calls += 1

    class Harness:
        def __init__(self) -> None:
            self.alas_name = "profile-a"
            self.alas_config = ConfigUpdater()
            self.save_calls = []

        def _save_config(self, changes, config_name, config_updater):
            self.save_calls.append((changes.copy(), config_name, config_updater))
            for key, value in changes.items():
                deep_set(config_updater.data, key, value)

        def _read_autoscan_config(self):
            return FleetPageMixin._read_autoscan_config(self)

    harness = Harness()
    toasts = []
    monkeypatch.setattr(
        fleet_page_module,
        "pin",
        {
            "FleetPage_AutoScanMode": "daily",
            "FleetPage_AutoScanFleets": [6, 2, 6],
        },
    )
    monkeypatch.setattr(
        fleet_page_module,
        "toast",
        lambda message, **kwargs: toasts.append((message, kwargs)),
    )

    FleetPageMixin._save_autoscan_config(harness)

    assert len(harness.save_calls) == 1
    changes, instance, updater = harness.save_calls[0]
    assert changes == {
        "Alas.FleetAutoScan.Mode": "daily",
        "Alas.FleetAutoScan.Fleets": [2, 6],
    }
    assert instance == "profile-a"
    assert updater is harness.alas_config
    assert harness.alas_config.load_calls == 1
    persisted = load_fleet_autoscan_config(harness.alas_config.data)
    assert persisted.mode.value == "daily"
    assert persisted.selection.fleet_indices == (2, 6)
    assert toasts[-1][1]["color"] == "success"


def test_autoscan_invalid_selection_never_enters_config_write_pipeline(
    monkeypatch,
) -> None:
    import module.webui.app_fleet_page as fleet_page_module

    class Harness:
        alas_name = "profile-a"
        alas_config = SimpleNamespace(load=lambda: None)

        def _save_config(self, *_args, **_kwargs):
            pytest.fail("Недопустимая selection не должна попадать в config write")

    toasts = []
    monkeypatch.setattr(
        fleet_page_module,
        "pin",
        {
            "FleetPage_AutoScanMode": "daily",
            "FleetPage_AutoScanFleets": [],
        },
    )
    monkeypatch.setattr(
        fleet_page_module,
        "toast",
        lambda message, **kwargs: toasts.append((message, kwargs)),
    )

    FleetPageMixin._save_autoscan_config(Harness())

    assert toasts[-1][1]["color"] == "error"


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
