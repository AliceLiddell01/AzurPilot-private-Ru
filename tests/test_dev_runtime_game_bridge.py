from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from module.application.game_models import DashboardResource, DashboardResources
from module.application.morale import (
    MoraleFleetState,
    MoraleKnowledge,
    MoraleSelectionState,
    MoraleSlotState,
)
from module.dev_runtime import (
    DevEnvironment,
    DevSessionManager,
    DevStatusKind,
    game_bridge,
)
from module.dev_runtime.game_bridge import (
    DevGameBridge,
    GameObservationCapability,
    GameObservationCapture,
    GameObservationError,
    GameObservationRegistry,
    GameObservationSnapshot,
    GameObservationStatus,
    GameObservationStore,
    MoraleObservationProvider,
    ObservationParameter,
    ObservationParameterType,
)
from module.dev_runtime.smoke import SmokeRunManager, SmokeSpec, SmokeStoreError
from module.dev_runtime.target import DevTarget
from module.formation.model import FleetSelection, FormationFleetSide

_NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
_TARGET = DevTarget("fixture-target")


class _SyntheticProvider:
    capability = GameObservationCapability(
        capability_id="synthetic",
        description="Синтетическая game projection",
        source="tests.synthetic",
        parameters=(
            ObservationParameter(
                name="fleet_index",
                value_type=ObservationParameterType.INTEGER,
                required=True,
                minimum=1,
                maximum=10,
            ),
        ),
    )

    def capture(
        self,
        _target: DevTarget,
        parameters: dict[str, object],
        *,
        captured_at: datetime,
    ) -> GameObservationCapture:
        return GameObservationCapture(
            status=GameObservationStatus.KNOWN,
            source=self.capability.source,
            provenance={
                "capability_id": self.capability.capability_id,
                "owner": "tests",
                "freshness": "synthetic",
            },
            payload={"fleet_index": parameters["fleet_index"], "captured_at": captured_at},
        )


class _ExplodingProvider(_SyntheticProvider):
    capability = GameObservationCapability(
        capability_id="exploding",
        description="Падающая game projection",
        source="tests.exploding",
    )

    def capture(
        self,
        target: DevTarget,
        parameters: dict[str, object],
        *,
        captured_at: datetime,
    ) -> GameObservationCapture:
        raise RuntimeError("внутренние сведения provider не должны пересекать bridge")


def _snapshot(
    *,
    smoke_id: str = "smoke-1",
    checkpoint_id: str = "before",
    session_id: str = "session-1",
    target: DevTarget = _TARGET,
    observation_id: str | None = None,
) -> GameObservationSnapshot:
    capture = GameObservationCapture(
        status=GameObservationStatus.KNOWN,
        source="tests.synthetic",
        provenance={"capability_id": "synthetic", "owner": "tests"},
        payload={"value": 7},
    )
    return GameObservationSnapshot.create(
        capture,
        target=target,
        checkpoint_id=checkpoint_id,
        session_id=session_id,
        smoke_id=smoke_id,
        captured_at=_NOW,
        observation_id=observation_id,
    )


def test_registry_is_sorted_strict_and_fail_closed_for_provider_errors() -> None:
    registry = GameObservationRegistry((_ExplodingProvider(), _SyntheticProvider()))

    # Дублирующий capability_id отклоняется до того, как второй provider затенит первый.
    with pytest.raises(GameObservationError) as duplicate:
        GameObservationRegistry((_SyntheticProvider(), _SyntheticProvider()))
    assert duplicate.value.code == "DEV_GAME_CAPABILITY_CONFLICT"

    assert [item.capability_id for item in registry.descriptors()] == ["exploding", "synthetic"]
    with pytest.raises(GameObservationError) as missing:
        registry.capture(
            target=_TARGET,
            capability_id="unknown",
            parameters={},
            captured_at=_NOW,
        )
    assert missing.value.code == "DEV_GAME_CAPABILITY_UNAVAILABLE"

    with pytest.raises(GameObservationError) as invalid:
        registry.capture(
            target=_TARGET,
            capability_id="synthetic",
            parameters={"fleet_index": 7, "unexpected": True},
            captured_at=_NOW,
        )
    assert invalid.value.code == "DEV_GAME_PARAMETERS_INVALID"

    with pytest.raises(GameObservationError) as invalid_key:
        registry.capture(
            target=_TARGET,
            capability_id="synthetic",
            parameters={1: 7},
            captured_at=_NOW,
        )
    assert invalid_key.value.code == "DEV_GAME_PARAMETERS_INVALID"

    unavailable = GameObservationRegistry((_ExplodingProvider(),)).capture(
        target=_TARGET,
        capability_id="exploding",
        parameters={},
        captured_at=_NOW,
    )
    assert unavailable.status is GameObservationStatus.UNAVAILABLE
    assert unavailable.payload == {"reason_code": "DEV_GAME_PROVIDER_UNAVAILABLE"}


def test_snapshot_checksum_and_target_binding_are_verified() -> None:
    snapshot = _snapshot()
    restored = GameObservationSnapshot.from_dict(snapshot.as_dict())

    assert restored == snapshot
    tampered = snapshot.as_dict()
    assert isinstance(tampered["payload"], dict)
    tampered["payload"]["value"] = 8
    with pytest.raises(GameObservationError) as checksum:
        GameObservationSnapshot.from_dict(tampered)
    assert checksum.value.code == "DEV_GAME_OBSERVATION_CHECKSUM_MISMATCH"

    with pytest.raises(GameObservationError) as target_error:
        GameObservationSnapshot.create(
            GameObservationCapture(
                status=GameObservationStatus.KNOWN,
                source="tests.synthetic",
                provenance={"capability_id": "synthetic"},
                payload={},
            ),
            target="fixture-target",  # type: ignore[arg-type]
            checkpoint_id="before",
            captured_at=_NOW,
        )
    assert target_error.value.code == "DEV_GAME_OBSERVATION_TARGET_INVALID"


def test_store_is_scoped_atomic_and_has_bounded_duplicate_policy(tmp_path: Path) -> None:
    environment = SimpleNamespace(repository_root=tmp_path)
    store = GameObservationStore(environment, "smoke-1")
    snapshot = _snapshot()

    assert store.append(snapshot) is True
    assert store.append(snapshot, duplicate_policy="keep_first") is False
    with pytest.raises(GameObservationError) as duplicate:
        store.append(snapshot)
    assert duplicate.value.code == "DEV_GAME_CHECKPOINT_DUPLICATE"
    assert store.read() == (snapshot,)
    assert store.read(checkpoint_id="before") == (snapshot,)
    assert store.summary()["relative_file"] == (
        "config/state/dev-runtime-smoke/smoke-1/game-observations.json"
    )

    raw = json.loads(store.path.read_text(encoding="utf-8"))
    raw["observations"].append(raw["observations"][0])
    store.path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(GameObservationError) as corrupt_duplicate:
        store.read()
    assert corrupt_duplicate.value.code == "DEV_GAME_OBSERVATION_CORRUPT"

    with pytest.raises(GameObservationError) as scope:
        store.append(_snapshot(smoke_id="other"))
    assert scope.value.code == "DEV_GAME_OBSERVATION_SCOPE_MISMATCH"


def test_store_summary_distinguishes_empty_and_heterogeneous_targets(tmp_path: Path) -> None:
    environment = SimpleNamespace(repository_root=tmp_path)
    empty = GameObservationStore(environment, "empty").summary()

    assert empty["count"] == 0
    assert empty["profile_count"] == 0
    assert empty["target_count"] == 0
    assert empty["profile_name"] is None
    assert empty["target_identity"] is None

    mixed_store = GameObservationStore(environment, "mixed")
    mixed_store.append(_snapshot(smoke_id="mixed", checkpoint_id="before"))
    mixed_store.append(
        _snapshot(
            smoke_id="mixed",
            checkpoint_id="final",
            target=DevTarget("stale-target"),
        )
    )
    mixed = mixed_store.summary()

    assert mixed["count"] == 2
    assert mixed["profile_count"] == 2
    assert mixed["target_count"] == 2
    assert mixed["profile_name"] is None
    assert mixed["target_identity"] is None


def test_store_rejects_missing_or_invalid_repository_root(tmp_path: Path) -> None:
    with pytest.raises(GameObservationError) as missing:
        GameObservationStore(SimpleNamespace(), "smoke-1")
    assert missing.value.code == "DEV_GAME_OBSERVATION_INVALID"

    with pytest.raises(GameObservationError) as invalid:
        GameObservationStore(SimpleNamespace(repository_root=object()), "smoke-1")
    assert invalid.value.code == "DEV_GAME_OBSERVATION_INVALID"


def test_store_rejects_corruption_path_traversal_and_snapshot_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = SimpleNamespace(repository_root=tmp_path)
    store = GameObservationStore(environment, "smoke-1")
    store.append(_snapshot())
    store.path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(GameObservationError) as corruption:
        store.read()
    assert corruption.value.code == "DEV_GAME_OBSERVATION_CORRUPT"

    with pytest.raises(GameObservationError) as traversal:
        GameObservationStore(environment, "../escape")
    assert traversal.value.code == "DEV_GAME_OBSERVATION_INVALID"

    monkeypatch.setattr(game_bridge, "GAME_OBSERVATION_MAX_SNAPSHOTS", 1)
    limited_store = GameObservationStore(environment, "limited")
    limited_store.append(_snapshot(smoke_id="limited", checkpoint_id="first"))
    with pytest.raises(GameObservationError) as limit:
        limited_store.append(_snapshot(smoke_id="limited", checkpoint_id="second"))
    assert limit.value.code == "DEV_GAME_OBSERVATION_LIMIT"


def test_morale_capability_caps_list_size_to_parameter_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        game_bridge,
        "SUPPORTED_SURFACE_FLEET_INDICES",
        tuple(range(1, game_bridge.GAME_OBSERVATION_MAX_LIST_ITEMS + 5)),
    )

    provider = MoraleObservationProvider(lambda: object())

    assert provider.capability.parameters[0].max_items == game_bridge.GAME_OBSERVATION_MAX_LIST_ITEMS


def test_smoke_target_binding_rejects_legacy_record_without_target(tmp_path: Path) -> None:
    environment = DevEnvironment(tmp_path, Path("python"), _TARGET)
    manager = SmokeRunManager(environment, now=lambda: _NOW)

    with pytest.raises(SmokeStoreError) as error:
        manager._bind_record_target(SimpleNamespace(target_profile=None, target_identity=None))  # type: ignore[arg-type]

    assert error.value.code == "DEV_SMOKE_TARGET_MISSING"


def test_smoke_checkpoint_rejects_provider_session_mismatch(tmp_path: Path) -> None:
    environment = DevEnvironment(tmp_path, Path("python"), _TARGET)
    bridge = SimpleNamespace(
        capture=lambda *_args, **_kwargs: _snapshot(session_id="session-1")
    )
    manager = SmokeRunManager(environment, game_bridge=bridge, now=lambda: _NOW)
    spec = SmokeSpec.model_validate(
        {
            "name": "game-bridge-smoke",
            "objective": "Проверить provenance checkpoint",
            "session": {"root_tasks": ["RootTask"]},
            "game_observations": {
                "observations": [{"capability_id": "synthetic"}],
            },
        }
    )

    # Public capture_smoke_game_checkpoint требует сохранённую SmokeRun; здесь
    # напрямую проверяется защита внутренней checkpoint boundary от чужой сессии.
    ok, _details, failure = manager._capture_game_checkpoint(
        SimpleNamespace(smoke_id="smoke-1"),
        spec,
        "before",
        "session-expected",
    )

    assert ok is False
    assert failure is not None
    assert failure.code == "DEV_GAME_OBSERVATION_TARGET_MISMATCH"
    assert not GameObservationStore(environment, "smoke-1").path.exists()


def test_smoke_checkpoint_exposes_persisted_game_evidence_refs(tmp_path: Path) -> None:
    environment = DevEnvironment(tmp_path, Path("python"), _TARGET)
    bridge = SimpleNamespace(
        capture=lambda *_args, **kwargs: _snapshot(
            smoke_id=kwargs["smoke_id"],
            checkpoint_id=kwargs["checkpoint_id"],
            session_id=kwargs["session_id"],
            observation_id="observation-1",
        )
    )
    manager = SmokeRunManager(environment, game_bridge=bridge, now=lambda: _NOW)
    spec = SmokeSpec.model_validate(
        {
            "name": "game-bridge-smoke",
            "objective": "Проверить ссылку на сохранённое observation",
            "session": {"root_tasks": ["RootTask"]},
            "game_observations": {
                "observations": [{"capability_id": "synthetic"}],
            },
        }
    )

    # Public capture_smoke_game_checkpoint не позволяет подменить bridge и
    # immutable SmokeRun; этот тест проверяет сохранение evidence через boundary.
    ok, details, failure = manager._capture_game_checkpoint(
        SimpleNamespace(smoke_id="smoke-1"),
        spec,
        "before",
        "session-1",
    )

    assert ok is True
    assert failure is None
    refs = details["game_observations"]["evidence_refs"]
    assert len(refs) == 1
    assert refs[0]["source"] == "game_observation"
    assert refs[0]["reference"].endswith("#observation-1")


def test_smoke_checkpoint_keep_first_uses_retained_status(tmp_path: Path) -> None:
    environment = DevEnvironment(tmp_path, Path("python"), _TARGET)

    class _ChangingBridge:
        def __init__(self) -> None:
            self.calls = 0

        def capture(
            self,
            _target: DevTarget,
            _capability_id: str,
            _parameters: dict[str, object],
            *,
            checkpoint_id: str,
            session_id: str,
            smoke_id: str,
            captured_at: datetime,
        ) -> GameObservationSnapshot:
            self.calls += 1
            status = (
                GameObservationStatus.KNOWN
                if self.calls == 1
                else GameObservationStatus.UNAVAILABLE
            )
            return GameObservationSnapshot.create(
                GameObservationCapture(
                    status=status,
                    source="tests.synthetic",
                    provenance={"capability_id": "synthetic"},
                    payload={"call": self.calls},
                ),
                target=_TARGET,
                checkpoint_id=checkpoint_id,
                session_id=session_id,
                smoke_id=smoke_id,
                captured_at=captured_at,
            )

    bridge = _ChangingBridge()
    manager = SmokeRunManager(environment, game_bridge=bridge, now=lambda: _NOW)
    spec = SmokeSpec.model_validate(
        {
            "name": "keep-first-smoke",
            "objective": "Проверить сохранение первого game observation",
            "session": {"root_tasks": ["RootTask"]},
            "game_observations": {
                "observations": [{"capability_id": "synthetic"}],
                "duplicate_policy": "keep_first",
            },
        }
    )
    record = SimpleNamespace(smoke_id="smoke-1")

    # Public capture_smoke_game_checkpoint не поддерживает повторную инъекцию
    # synthetic bridge для проверки duplicate_policy keep_first.
    first_ok, _first_details, first_failure = manager._capture_game_checkpoint(
        record,
        spec,
        "before",
        "session-1",
    )
    second_ok, second_details, second_failure = manager._capture_game_checkpoint(
        record,
        spec,
        "before",
        "session-1",
    )

    assert first_ok is True
    assert first_failure is None
    assert second_ok is True
    assert second_failure is None
    assert second_details["game_observations"]["stored"] == 0
    assert second_details["game_observations"]["checkpoint_statuses"] == ["known"]


def test_manager_rejects_stale_standalone_provider_target(tmp_path: Path) -> None:
    environment = DevEnvironment(tmp_path, Path("python"), _TARGET)
    bridge = SimpleNamespace(
        capture=lambda *_args, **_kwargs: _snapshot(
            session_id=None,
            target=DevTarget("stale-target"),
        )
    )
    manager = DevSessionManager(
        environment,
        target_locked=True,
        game_bridge_factory=lambda _environment: bridge,
        now=lambda: _NOW,
    )

    result = manager.get_game_observation("synthetic", {})

    assert result.ok is False
    assert result.code == "DEV_GAME_OBSERVATION_TARGET_MISMATCH"


def test_manager_preserves_state_for_unknown_database_check(tmp_path: Path) -> None:
    environment = DevEnvironment(tmp_path, Path("python"), _TARGET)

    class _Diagnostics:
        def list_checks(self) -> tuple[object, ...]:
            return (SimpleNamespace(check_id="known-check"),)

        def run_check(self, _check_id: str, _target_profile: str) -> object:
            raise ValueError("Неизвестный database diagnostic check")

    manager = DevSessionManager(
        environment,
        target_locked=True,
        database_diagnostics_factory=lambda _environment: _Diagnostics(),
    )

    result = manager.run_database_check("missing-check")

    assert result.ok is False
    assert result.code == "DEV_DATABASE_CHECK_UNKNOWN"
    assert result.state == DevStatusKind.NO_SESSION.value


def test_manager_distinguishes_invalid_database_target_from_unknown_check(tmp_path: Path) -> None:
    environment = DevEnvironment(tmp_path, Path("python"), _TARGET)

    class _Diagnostics:
        def list_checks(self) -> tuple[object, ...]:
            return (SimpleNamespace(check_id="connectivity"),)

        def run_check(self, _check_id: str, _target_profile: str) -> object:
            raise ValueError("target_profile имеет недопустимый формат")

    manager = DevSessionManager(
        environment,
        target_locked=True,
        database_diagnostics_factory=lambda _environment: _Diagnostics(),
    )

    result = manager.run_database_check("connectivity")

    assert result.ok is False
    assert result.code == "DEV_DATABASE_TARGET_INVALID"
    assert result.state == DevStatusKind.NO_SESSION.value


def test_manager_validates_database_repair_session_before_echo(tmp_path: Path) -> None:
    environment = DevEnvironment(tmp_path, Path("python"), _TARGET)
    manager = DevSessionManager(environment, target_locked=True)

    result = manager.preview_database_repair("none", session_id="../foreign")

    assert result.ok is False
    assert result.code == "DEV_SESSION_ID_INVALID"


def test_manager_validates_database_repair_id_before_echo(tmp_path: Path) -> None:
    environment = DevEnvironment(tmp_path, Path("python"), _TARGET)
    manager = DevSessionManager(environment, target_locked=True)

    result = manager.preview_database_repair("../secret", session_id=None)

    assert result.ok is False
    assert result.code == "DEV_DATABASE_REPAIR_ID_INVALID"
    assert "secret" not in str(result.as_dict())


def test_resources_provider_uses_typed_application_projection() -> None:
    seen: list[str] = []

    class _GameReadService:
        def get_resources(self, instance: str) -> DashboardResources:
            seen.append(instance)
            return DashboardResources(
                (
                    DashboardResource(
                        key="oil",
                        label="Нефть",
                        value=123,
                        limit=1000,
                    ),
                )
            )

    bridge = DevGameBridge(
        game_read_service_factory=_GameReadService,
        morale_service_factory=None,
    )
    snapshot = bridge.capture(
        _TARGET,
        "resources",
        captured_at=_NOW,
    )

    assert snapshot.status is GameObservationStatus.KNOWN
    assert seen == ["fixture-target"]
    assert snapshot.as_dict()["payload"]["items"] == [
        {
            "key": "oil",
            "label": "Нефть",
            "value": 123,
            "limit": 1000,
            "total": None,
            "last_update": None,
        }
    ]


def test_morale_provider_preserves_typed_unknown_without_inventing_baseline() -> None:
    slots = tuple(
        MoraleSlotState(
            fleet_index=1,
            side=side,
            position=position,
            occupied=None,
            identity_status=None,
            canonical_identity=None,
            canonical_name=None,
            ship_form=None,
            knowledge=MoraleKnowledge.UNKNOWN,
        )
        for side, position in (
            (FormationFleetSide.MAIN, 1),
            (FormationFleetSide.MAIN, 2),
            (FormationFleetSide.MAIN, 3),
            (FormationFleetSide.VANGUARD, 1),
            (FormationFleetSide.VANGUARD, 2),
            (FormationFleetSide.VANGUARD, 3),
        )
    )
    state = MoraleSelectionState(
        selection=FleetSelection.one(1),
        fleets=(MoraleFleetState(1, None, None, slots),),
        projected_at=_NOW,
    )
    calls: list[tuple[str, FleetSelection, datetime]] = []

    class _MoraleService:
        def state_read_only(
            self,
            instance: str,
            selection: FleetSelection,
            *,
            at: datetime,
        ) -> MoraleSelectionState:
            calls.append((instance, selection, at))
            return state

    snapshot = MoraleObservationProvider(lambda: _MoraleService()).capture(
        _TARGET,
        {"fleet_indices": [1]},
        captured_at=_NOW,
    )

    assert snapshot.status is GameObservationStatus.UNKNOWN
    assert calls == [("fixture-target", FleetSelection.one(1), _NOW)]
    payload = snapshot.payload
    assert payload["selection"] == (1,)
    assert all(slot["baseline"] is None for slot in payload["fleets"][0]["slots"])


def test_smoke_game_observations_have_named_intermediates_and_reserved_boundaries() -> None:
    spec = SmokeSpec.model_validate(
        {
            "name": "game-bridge-smoke",
            "objective": "Проверить game observation checkpoints",
            "session": {"root_tasks": ["RootTask"]},
            "game_observations": {
                "observations": [{"capability_id": "resources"}],
                "checkpoints": [
                    {
                        "checkpoint_id": "midpoint",
                        "observations": [{"capability_id": "morale", "parameters": {"fleet_indices": [1]}}],
                    }
                ],
                "duplicate_policy": "keep_first",
            },
        },
        strict=True,
    )
    assert spec.game_observations is not None
    assert spec.game_observations.checkpoints[0].checkpoint_id == "midpoint"
    assert spec.game_observations.duplicate_policy == "keep_first"

    with pytest.raises(ValidationError) as reserved:
        SmokeSpec.model_validate(
            {
                "name": "reserved-checkpoint",
                "objective": "invalid",
                "session": {"root_tasks": ["RootTask"]},
                "game_observations": {
                    "observations": [{"capability_id": "resources"}],
                    "checkpoints": [
                        {
                            "checkpoint_id": "before",
                            "observations": [{"capability_id": "resources"}],
                        }
                    ],
                },
            },
            strict=True,
        )
    assert any("checkpoint_id" in str(error["loc"]) for error in reserved.value.errors())


def test_runtime_game_bridge_uses_read_only_persistence_for_morale_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import module.application.morale as morale_module
    import module.persistence.runtime as persistence_runtime
    from module.application import runtime_storage

    calls: list[str] = []

    class _Composition:
        engine = object()

        def uow_factory(self) -> object:
            return object()

        def dispose(self) -> None:
            calls.append("dispose")

    class _MoraleService:
        def __init__(self, _uow_factory: object, *, clock: object = None) -> None:
            calls.append("morale")

    def _read_only_composition(_environment: object) -> _Composition:
        calls.append("read_only_composition")
        return _Composition()

    monkeypatch.setattr(
        persistence_runtime,
        "build_read_only_persistence_composition",
        _read_only_composition,
    )
    monkeypatch.setattr(
        persistence_runtime,
        "bootstrap_runtime_storage",
        lambda **_kwargs: pytest.fail("Morale observation не должен запускать production bootstrap"),
    )
    production_provider = object()
    monkeypatch.setattr(runtime_storage, "_provider", production_provider)
    monkeypatch.setattr(morale_module, "MoraleService", _MoraleService)

    bridge = game_bridge.build_runtime_game_bridge(
        SimpleNamespace(repository_root=Path.cwd()),
    )
    # Публичный bridge не отдаёт фабрику сервиса; read-only composition требует
    # проверки этой внутренней границы без запуска production bootstrap.
    provider = bridge.registry._providers["morale"]
    service = provider._service_factory()

    assert isinstance(service, _MoraleService)
    assert calls == ["read_only_composition", "morale"]
    assert runtime_storage._provider is production_provider
    bridge.dispose()
    assert calls == ["read_only_composition", "morale", "dispose"]
    with pytest.raises(GameObservationError) as disposed:
        bridge.capture(_TARGET, "morale", {"fleet_indices": [1]}, captured_at=_NOW)
    assert disposed.value.code == "DEV_GAME_OBSERVATION_PROVIDER_UNAVAILABLE"
