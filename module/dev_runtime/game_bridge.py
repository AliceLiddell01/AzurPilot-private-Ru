"""Односторонний Dev → application bridge и ограниченные game observations.

Модуль не содержит MCP types и не владеет игровым lifecycle.  Он получает
typed application services через composition root, фиксирует только
target-bound projections и сохраняет snapshots в уже принадлежащей Smoke
границе.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast

from module.application.game_models import (
    DashboardResources,
    freeze_payload,
    thaw_payload,
)
from module.application.game_read_service import GameReadService
from module.application.morale import MoraleSelectionState
from module.dev_runtime.bounded_io import BoundedReadTooLarge, read_bounded_bytes
from module.dev_runtime.target import DevTarget, target_identity
from module.dev_runtime.task_sandbox import (
    _atomic_json_write,
    _ensure_scoped_path,
    _exclusive_policy_lock,
    _is_reparse_point,
)
from module.formation.model import SUPPORTED_SURFACE_FLEET_INDICES, FleetSelection

GAME_OBSERVATION_SCHEMA_VERSION = 1
GAME_OBSERVATION_MAX_PAYLOAD_BYTES = 64 * 1024
# Один SmokeSpec может объявить до 8 capabilities и до 8 named
# intermediate checkpoints: before + final + 8 * 8 snapshots.
GAME_OBSERVATION_MAX_SNAPSHOTS = 128
GAME_OBSERVATION_MAX_STORE_BYTES = 8 * 1024 * 1024
GAME_OBSERVATION_MAX_CAPABILITIES = 32
GAME_OBSERVATION_MAX_PARAMETERS = 16
GAME_OBSERVATION_MAX_PROVENANCE_FIELDS = 16
GAME_OBSERVATION_MAX_PARAMETER_VALUE = 10**12
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_SHA = re.compile(r"^[0-9a-f]{64}$")
_SAFE_TEXT = re.compile(r"^[^\x00-\x1f\x7f]{1,256}$")


class GameObservationError(ValueError):
    """Безопасная ошибка registry/bridge без raw provider details."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self)}


class GameObservationStatus(StrEnum):
    KNOWN = "known"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


class ObservationParameterType(StrEnum):
    INTEGER = "integer"
    INTEGER_LIST = "integer_list"
    STRING = "string"
    BOOLEAN = "boolean"


def _text(value: object, *, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise GameObservationError("DEV_GAME_OBSERVATION_INVALID", f"{field} имеет недопустимый формат")
    if value != value.strip() or not _SAFE_TEXT.fullmatch(value):
        raise GameObservationError("DEV_GAME_OBSERVATION_INVALID", f"{field} имеет недопустимый формат")
    return value


def _identifier(value: object, *, field: str) -> str:
    value = _text(value, field=field, maximum=128)
    if not _SAFE_ID.fullmatch(value):
        raise GameObservationError("DEV_GAME_OBSERVATION_INVALID", f"{field} имеет небезопасный формат")
    return value


def _aware(value: object, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise GameObservationError("DEV_GAME_OBSERVATION_INVALID", f"{field} должен быть timezone-aware datetime")
    return value.astimezone(UTC)


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    return value


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            _json_value(value),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise GameObservationError(
            "DEV_GAME_OBSERVATION_PAYLOAD_INVALID",
            "Наблюдение содержит неподдерживаемое значение",
        ) from exc


@dataclass(frozen=True, slots=True)
class ObservationParameter:
    name: str
    value_type: ObservationParameterType
    required: bool = False
    minimum: int | None = None
    maximum: int | None = None
    max_items: int | None = None

    def __post_init__(self) -> None:
        _identifier(self.name, field="parameter.name")
        if not isinstance(self.value_type, ObservationParameterType):
            raise TypeError("value_type должен быть ObservationParameterType")
        if type(self.required) is not bool:
            raise TypeError("required должен быть bool")
        if self.minimum is not None and type(self.minimum) is not int:
            raise TypeError("minimum должен быть int или None")
        if self.maximum is not None and type(self.maximum) is not int:
            raise TypeError("maximum должен быть int или None")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum не должен быть больше maximum")
        if self.max_items is not None and (type(self.max_items) is not int or not 1 <= self.max_items <= GAME_OBSERVATION_MAX_PARAMETERS):
            raise ValueError("max_items имеет недопустимое значение")

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "name": self.name,
            "value_type": self.value_type.value,
            "required": self.required,
        }
        if self.minimum is not None:
            result["minimum"] = self.minimum
        if self.maximum is not None:
            result["maximum"] = self.maximum
        if self.max_items is not None:
            result["max_items"] = self.max_items
        return result


@dataclass(frozen=True, slots=True)
class GameObservationCapability:
    capability_id: str
    description: str
    source: str
    parameters: tuple[ObservationParameter, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.capability_id, field="capability_id")
        _text(self.description, field="description")
        _identifier(self.source.replace(".", "_"), field="source")
        if not isinstance(self.parameters, tuple) or len(self.parameters) > GAME_OBSERVATION_MAX_PARAMETERS:
            raise ValueError("parameters имеет недопустимый размер")
        if any(not isinstance(item, ObservationParameter) for item in self.parameters):
            raise TypeError("parameters должен содержать ObservationParameter")
        names = tuple(item.name for item in self.parameters)
        if len(names) != len(set(names)):
            raise ValueError("parameters не должен содержать дубликаты")

    def as_dict(self) -> dict[str, object]:
        return {
            "capability_id": self.capability_id,
            "kind": "game_observation",
            "description": self.description,
            "source": self.source,
            "parameters": [item.as_dict() for item in self.parameters],
        }


@dataclass(frozen=True, slots=True)
class GameObservationCapture:
    status: GameObservationStatus
    source: str
    provenance: Mapping[str, object]
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.status, GameObservationStatus):
            raise TypeError("status должен быть GameObservationStatus")
        _text(self.source, field="source")
        provenance = freeze_payload(self.provenance, field_name="provenance")
        payload = freeze_payload(self.payload, field_name="payload")
        if not isinstance(provenance, Mapping) or not isinstance(payload, Mapping):
            raise TypeError("provenance и payload должны быть mapping")
        if len(provenance) > GAME_OBSERVATION_MAX_PROVENANCE_FIELDS:
            raise ValueError("provenance имеет слишком много полей")
        object.__setattr__(self, "provenance", provenance)
        object.__setattr__(self, "payload", payload)


@dataclass(frozen=True, slots=True)
class GameObservationSnapshot:
    schema_version: int
    observation_id: str
    session_id: str | None
    smoke_id: str | None
    profile_name: str
    target_identity: str
    checkpoint_id: str
    capability_id: str
    captured_at: datetime
    status: GameObservationStatus
    source: str
    provenance: Mapping[str, object]
    payload: Mapping[str, object]
    sha256: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != GAME_OBSERVATION_SCHEMA_VERSION:
            raise GameObservationError("DEV_GAME_OBSERVATION_SCHEMA_UNSUPPORTED", "Версия game observation не поддерживается")
        _identifier(self.observation_id, field="observation_id")
        if self.session_id is not None:
            _identifier(self.session_id, field="session_id")
        if self.smoke_id is not None:
            _identifier(self.smoke_id, field="smoke_id")
        try:
            target = DevTarget(self.profile_name)
        except ValueError as exc:
            raise GameObservationError("DEV_GAME_OBSERVATION_TARGET_INVALID", "Профиль observation имеет небезопасный формат") from exc
        if self.target_identity != target_identity(target):
            raise GameObservationError("DEV_GAME_OBSERVATION_TARGET_MISMATCH", "Идентичность observation не совпадает с target")
        _identifier(self.checkpoint_id, field="checkpoint_id")
        _identifier(self.capability_id, field="capability_id")
        _aware(self.captured_at, field="captured_at")
        if not isinstance(self.status, GameObservationStatus):
            raise TypeError("status должен быть GameObservationStatus")
        _text(self.source, field="source")
        provenance = freeze_payload(self.provenance, field_name="provenance")
        payload = freeze_payload(self.payload, field_name="payload")
        if not isinstance(provenance, Mapping) or not isinstance(payload, Mapping):
            raise TypeError("provenance и payload должны быть mapping")
        if len(provenance) > GAME_OBSERVATION_MAX_PROVENANCE_FIELDS:
            raise ValueError("provenance имеет слишком много полей")
        object.__setattr__(self, "captured_at", self.captured_at.astimezone(UTC))
        object.__setattr__(self, "provenance", provenance)
        object.__setattr__(self, "payload", payload)
        if not _SAFE_SHA.fullmatch(self.sha256):
            raise GameObservationError("DEV_GAME_OBSERVATION_CHECKSUM_INVALID", "Наблюдение имеет неверный checksum")
        if self.checksum() != self.sha256:
            raise GameObservationError("DEV_GAME_OBSERVATION_CHECKSUM_MISMATCH", "Checksum observation не совпадает с payload")
        if len(self.canonical_json().encode("utf-8")) > GAME_OBSERVATION_MAX_PAYLOAD_BYTES:
            raise GameObservationError("DEV_GAME_OBSERVATION_TOO_LARGE", "Наблюдение превышает ограничение размера")

    @classmethod
    def create(
        cls,
        capture: GameObservationCapture,
        *,
        target: DevTarget,
        checkpoint_id: str,
        session_id: str | None = None,
        smoke_id: str | None = None,
        captured_at: datetime,
        observation_id: str | None = None,
    ) -> GameObservationSnapshot:
        if not isinstance(capture, GameObservationCapture):
            raise TypeError("capture должен быть GameObservationCapture")
        if not isinstance(target, DevTarget):
            raise GameObservationError("DEV_GAME_OBSERVATION_TARGET_INVALID", "Snapshot получил некорректный target")
        identity = target_identity(target)
        probe = {
            "schema_version": GAME_OBSERVATION_SCHEMA_VERSION,
            "observation_id": observation_id or str(uuid.uuid4()),
            "session_id": session_id,
            "smoke_id": smoke_id,
            "profile_name": target.profile_name,
            "target_identity": identity,
            "checkpoint_id": checkpoint_id,
            "capability_id": "pending",
            "captured_at": _aware(captured_at, field="captured_at"),
            "status": capture.status,
            "source": capture.source,
            "provenance": capture.provenance,
            "payload": capture.payload,
        }
        capability_id = capture.provenance.get("capability_id")
        if not isinstance(capability_id, str):
            raise GameObservationError("DEV_GAME_OBSERVATION_PROVIDER_INVALID", "Provider не указал capability_id")
        probe["capability_id"] = capability_id
        canonical = _canonical_json(probe)
        return cls(**probe, sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest())

    def canonical_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "observation_id": self.observation_id,
            "session_id": self.session_id,
            "smoke_id": self.smoke_id,
            "profile_name": self.profile_name,
            "target_identity": self.target_identity,
            "checkpoint_id": self.checkpoint_id,
            "capability_id": self.capability_id,
            "captured_at": self.captured_at,
            "status": self.status,
            "source": self.source,
            "provenance": self.provenance,
            "payload": self.payload,
        }

    def canonical_json(self) -> str:
        return _canonical_json(self.canonical_dict())

    def checksum(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            **cast(dict[str, object], _json_value(self.canonical_dict())),
            "status": self.status.value,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> GameObservationSnapshot:
        if not isinstance(value, Mapping):
            raise GameObservationError("DEV_GAME_OBSERVATION_CORRUPT", "Snapshot имеет неверную структуру")
        required = {
            "schema_version",
            "observation_id",
            "session_id",
            "smoke_id",
            "profile_name",
            "target_identity",
            "checkpoint_id",
            "capability_id",
            "captured_at",
            "status",
            "source",
            "provenance",
            "payload",
            "sha256",
        }
        if set(value) != required:
            raise GameObservationError("DEV_GAME_OBSERVATION_CORRUPT", "Snapshot имеет неподдерживаемую схему")
        try:
            captured_at = value["captured_at"]
            if isinstance(captured_at, str):
                captured_at = datetime.fromisoformat(captured_at)
            status = GameObservationStatus(value["status"])
            return cls(
                schema_version=value["schema_version"],
                observation_id=value["observation_id"],
                session_id=value["session_id"],
                smoke_id=value["smoke_id"],
                profile_name=value["profile_name"],
                target_identity=value["target_identity"],
                checkpoint_id=value["checkpoint_id"],
                capability_id=value["capability_id"],
                captured_at=captured_at,
                status=status,
                source=value["source"],
                provenance=value["provenance"],
                payload=value["payload"],
                sha256=value["sha256"],
            )
        except GameObservationError:
            raise
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise GameObservationError("DEV_GAME_OBSERVATION_CORRUPT", "Snapshot невозможно проверить") from exc


class GameObservationProvider(Protocol):
    @property
    def capability(self) -> GameObservationCapability: ...

    def capture(
        self,
        target: DevTarget,
        parameters: Mapping[str, object],
        *,
        captured_at: datetime,
    ) -> GameObservationCapture: ...


class GameObservationRegistry:
    """Строгий registry providers без динамического импорта/исполнения."""

    def __init__(self, providers: Sequence[GameObservationProvider] = ()) -> None:
        self._providers: dict[str, GameObservationProvider] = {}
        for provider in providers:
            self.register(provider)

    def register(self, provider: GameObservationProvider) -> None:
        try:
            capability = provider.capability
        except Exception as exc:
            raise GameObservationError("DEV_GAME_CAPABILITY_INVALID", "Provider не предоставил корректную capability") from exc
        if not isinstance(capability, GameObservationCapability):
            raise GameObservationError("DEV_GAME_CAPABILITY_INVALID", "Provider не предоставил корректную capability")
        if capability.capability_id in self._providers:
            raise GameObservationError("DEV_GAME_CAPABILITY_CONFLICT", "Такая game capability уже зарегистрирована")
        if len(self._providers) >= GAME_OBSERVATION_MAX_CAPABILITIES:
            raise GameObservationError("DEV_GAME_CAPABILITY_LIMIT", "Реестр game capabilities переполнен")
        self._providers[capability.capability_id] = provider

    def descriptors(self) -> tuple[GameObservationCapability, ...]:
        return tuple(self._providers[key].capability for key in sorted(self._providers))

    def _provider(self, capability_id: object) -> GameObservationProvider:
        capability_id = _identifier(capability_id, field="capability_id")
        try:
            return self._providers[capability_id]
        except KeyError as exc:
            raise GameObservationError("DEV_GAME_CAPABILITY_UNAVAILABLE", "Game capability отсутствует в registry") from exc

    @staticmethod
    def _validate_value(parameter: ObservationParameter, value: object) -> object:
        if parameter.value_type is ObservationParameterType.INTEGER:
            if type(value) is not int:
                raise GameObservationError("DEV_GAME_PARAMETERS_INVALID", f"Параметр {parameter.name} должен быть integer")
            if abs(value) > GAME_OBSERVATION_MAX_PARAMETER_VALUE:
                raise GameObservationError("DEV_GAME_PARAMETERS_INVALID", f"Параметр {parameter.name} выходит за bounded range")
            if parameter.minimum is not None and value < parameter.minimum:
                raise GameObservationError("DEV_GAME_PARAMETERS_INVALID", f"Параметр {parameter.name} меньше допустимого")
            if parameter.maximum is not None and value > parameter.maximum:
                raise GameObservationError("DEV_GAME_PARAMETERS_INVALID", f"Параметр {parameter.name} больше допустимого")
            return value
        if parameter.value_type is ObservationParameterType.INTEGER_LIST:
            if not isinstance(value, (list, tuple)) or not value:
                raise GameObservationError("DEV_GAME_PARAMETERS_INVALID", f"Параметр {parameter.name} должен быть непустым списком integer")
            if parameter.max_items is not None and len(value) > parameter.max_items:
                raise GameObservationError("DEV_GAME_PARAMETERS_INVALID", f"Параметр {parameter.name} превышает ограничение размера")
            normalized = []
            for item in value:
                normalized.append(GameObservationRegistry._validate_value(
                    ObservationParameter(
                        name=parameter.name,
                        value_type=ObservationParameterType.INTEGER,
                        minimum=parameter.minimum,
                        maximum=parameter.maximum,
                    ),
                    item,
                ))
            if len(set(normalized)) != len(normalized):
                raise GameObservationError("DEV_GAME_PARAMETERS_INVALID", f"Параметр {parameter.name} содержит дубликаты")
            return normalized
        if parameter.value_type is ObservationParameterType.STRING:
            return _text(value, field=f"parameters.{parameter.name}")
        if parameter.value_type is ObservationParameterType.BOOLEAN:
            if type(value) is not bool:
                raise GameObservationError("DEV_GAME_PARAMETERS_INVALID", f"Параметр {parameter.name} должен быть boolean")
            return value
        raise GameObservationError("DEV_GAME_PARAMETERS_INVALID", f"Параметр {parameter.name} имеет неизвестный тип")

    def validate_request(self, capability_id: object, parameters: object = None) -> dict[str, object]:
        provider = self._provider(capability_id)
        if parameters is None:
            parameters = {}
        if not isinstance(parameters, Mapping):
            raise GameObservationError("DEV_GAME_PARAMETERS_INVALID", "parameters должен быть объектом")
        if len(parameters) > GAME_OBSERVATION_MAX_PARAMETERS:
            raise GameObservationError("DEV_GAME_PARAMETERS_INVALID", "parameters содержит слишком много полей")
        if any(not isinstance(key, str) for key in parameters):
            raise GameObservationError("DEV_GAME_PARAMETERS_INVALID", "parameters содержит некорректное имя поля")
        definitions = {item.name: item for item in provider.capability.parameters}
        unknown = set(parameters) - set(definitions)
        if unknown:
            raise GameObservationError("DEV_GAME_PARAMETERS_INVALID", "parameters содержит неизвестное поле")
        missing = [name for name, item in definitions.items() if item.required and name not in parameters]
        if missing:
            raise GameObservationError("DEV_GAME_PARAMETERS_INVALID", "parameters не содержит обязательное поле")
        validated = {
            name: self._validate_value(definition, parameters[name])
            for name, definition in definitions.items()
            if name in parameters
        }
        return validated

    def capture(
        self,
        *,
        target: DevTarget,
        capability_id: object,
        parameters: object = None,
        checkpoint_id: str = "standalone",
        session_id: str | None = None,
        smoke_id: str | None = None,
        captured_at: datetime,
    ) -> GameObservationSnapshot:
        if not isinstance(target, DevTarget):
            raise GameObservationError("DEV_GAME_OBSERVATION_TARGET_INVALID", "Registry получил некорректный target")
        provider = self._provider(capability_id)
        validated = self.validate_request(capability_id, parameters)
        captured_at = _aware(captured_at, field="captured_at")
        try:
            capture = provider.capture(target, validated, captured_at=captured_at)
        except GameObservationError:
            raise
        except Exception as exc:  # noqa: BLE001 - provider boundary is fail-closed and sanitized.
            capture = GameObservationCapture(
                status=GameObservationStatus.UNAVAILABLE,
                source=provider.capability.source,
                provenance={
                    "capability_id": provider.capability.capability_id,
                    "owner": "DevGameBridge",
                    "reason_code": "DEV_GAME_PROVIDER_UNAVAILABLE",
                },
                payload={"reason_code": "DEV_GAME_PROVIDER_UNAVAILABLE"},
            )
            _ = exc
        if not isinstance(capture, GameObservationCapture):
            raise GameObservationError("DEV_GAME_OBSERVATION_PROVIDER_INVALID", "Provider вернул некорректное observation")
        if capture.provenance.get("capability_id") != provider.capability.capability_id:
            raise GameObservationError("DEV_GAME_OBSERVATION_PROVIDER_INVALID", "Provider вернул другую capability")
        return GameObservationSnapshot.create(
            capture,
            target=target,
            checkpoint_id=checkpoint_id,
            session_id=session_id,
            smoke_id=smoke_id,
            captured_at=captured_at,
        )


class ResourcesObservationProvider:
    """Provider поверх Stage 9 GameReadService, без чтения config напрямую."""

    def __init__(self, service_factory: Callable[[], GameReadService]) -> None:
        self._service_factory = service_factory
        self._capability = GameObservationCapability(
            capability_id="resources",
            description="Текущее typed dashboard resource projection назначенного target",
            source="application.game_read_service",
        )

    @property
    def capability(self) -> GameObservationCapability:
        return self._capability

    def capture(
        self,
        target: DevTarget,
        parameters: Mapping[str, object],
        *,
        captured_at: datetime,
    ) -> GameObservationCapture:
        if parameters:
            raise GameObservationError("DEV_GAME_PARAMETERS_INVALID", "resources не принимает параметры")
        resources = self._service_factory().get_resources(target.profile_name)
        if not isinstance(resources, DashboardResources):
            raise GameObservationError("DEV_GAME_OBSERVATION_PROVIDER_INVALID", "GameReadService вернул некорректные resources")
        payload = {
            "items": [
                {
                    "key": item.key,
                    "label": item.label,
                    "value": thaw_payload(item.value),
                    "limit": thaw_payload(item.limit),
                    "total": thaw_payload(item.total),
                    "last_update": thaw_payload(item.last_update),
                }
                for item in resources.items
            ]
        }
        return GameObservationCapture(
            status=GameObservationStatus.KNOWN,
            source=self.capability.source,
            provenance={
                "capability_id": self.capability.capability_id,
                "owner": "GameReadService",
                "freshness": "source_read",
            },
            payload=payload,
        )


class MoraleObservationProvider:
    """Provider typed per-ship morale projection через MoraleService."""

    def __init__(self, service_factory: Callable[[], object]) -> None:
        self._service_factory = service_factory
        self._capability = GameObservationCapability(
            capability_id="morale",
            description="Typed per-ship morale projection из Fleet State и persistence",
            source="application.morale_service",
            parameters=(
                ObservationParameter(
                    name="fleet_indices",
                    value_type=ObservationParameterType.INTEGER_LIST,
                    required=True,
                    minimum=min(SUPPORTED_SURFACE_FLEET_INDICES),
                    maximum=max(SUPPORTED_SURFACE_FLEET_INDICES),
                    max_items=len(SUPPORTED_SURFACE_FLEET_INDICES),
                ),
            ),
        )

    @property
    def capability(self) -> GameObservationCapability:
        return self._capability

    @staticmethod
    def _state_payload(state: MoraleSelectionState) -> dict[str, object]:
        return {
            "selection": list(state.selection.fleet_indices),
            "projected_at": state.projected_at,
            "fleets": [
                {
                    "fleet_index": fleet.fleet_index,
                    "formation_observation_id": fleet.formation_observation_id,
                    "formation_observed_at": fleet.formation_observed_at,
                    "slots": [
                        {
                            "side": slot.side.value,
                            "position": slot.position,
                            "occupied": slot.occupied,
                            "identity_status": slot.identity_status.value if slot.identity_status is not None else None,
                            "canonical_identity_key": (
                                slot.canonical_identity.key
                                if slot.canonical_identity is not None
                                else None
                            ),
                            "canonical_name": slot.canonical_name,
                            "ship_form": slot.ship_form.value if slot.ship_form is not None else None,
                            "knowledge": slot.knowledge.value,
                            "baseline": slot.baseline,
                            "current": slot.current,
                            "observed_at": slot.observed_at,
                            "source": slot.source,
                            "location": slot.location.value,
                            "dorm_scan_id": slot.dorm_scan_id,
                        }
                        for slot in fleet.slots
                    ],
                }
                for fleet in state.fleets
            ],
        }

    def capture(
        self,
        target: DevTarget,
        parameters: Mapping[str, object],
        *,
        captured_at: datetime,
    ) -> GameObservationCapture:
        indices = parameters.get("fleet_indices")
        if not isinstance(indices, list) or any(type(item) is not int for item in indices):
            raise GameObservationError("DEV_GAME_PARAMETERS_INVALID", "fleet_indices имеет некорректный формат")
        selection = FleetSelection(tuple(indices))
        service = self._service_factory()
        reader = getattr(service, "state_read_only", None)
        if not callable(reader):
            raise GameObservationError("DEV_GAME_OBSERVATION_PROVIDER_UNAVAILABLE", "MoraleService не предоставляет read-only state")
        state = reader(target.profile_name, selection, at=captured_at)
        if not isinstance(state, MoraleSelectionState):
            raise GameObservationError("DEV_GAME_OBSERVATION_PROVIDER_INVALID", "MoraleService вернул некорректное состояние")
        payload = self._state_payload(state)
        has_unknown_slot = any(
            slot.knowledge.value == "unknown"
            for fleet in state.fleets
            for slot in fleet.slots
        )
        return GameObservationCapture(
            status=(
                GameObservationStatus.UNKNOWN
                if has_unknown_slot
                else GameObservationStatus.KNOWN
            ),
            source=self.capability.source,
            provenance={
                "capability_id": self.capability.capability_id,
                "owner": "MoraleService",
                "freshness": "persisted_projection",
                **({"reason_code": "DEV_GAME_MORALE_UNKNOWN"} if has_unknown_slot else {}),
            },
            payload=payload,
        )


class DevGameBridge:
    """Target-bound bridge, не владеющий MCP transport и lifecycle."""

    def __init__(
        self,
        registry: GameObservationRegistry | None = None,
        *,
        game_read_service_factory: Callable[[], GameReadService] | None = None,
        morale_service_factory: Callable[[], object] | None = None,
    ) -> None:
        if registry is not None and (game_read_service_factory is not None or morale_service_factory is not None):
            raise ValueError("registry нельзя совмещать с provider factories")
        if registry is None:
            providers: list[GameObservationProvider] = []
            if game_read_service_factory is not None:
                providers.append(ResourcesObservationProvider(game_read_service_factory))
            if morale_service_factory is not None:
                providers.append(MoraleObservationProvider(morale_service_factory))
            registry = GameObservationRegistry(providers)
        self.registry = registry

    def descriptors(self) -> tuple[GameObservationCapability, ...]:
        return self.registry.descriptors()

    def validate_request(self, capability_id: object, parameters: object = None) -> dict[str, object]:
        return self.registry.validate_request(capability_id, parameters)

    def capture(
        self,
        target: DevTarget,
        capability_id: object,
        parameters: object = None,
        *,
        checkpoint_id: str = "standalone",
        session_id: str | None = None,
        smoke_id: str | None = None,
        captured_at: datetime,
    ) -> GameObservationSnapshot:
        if not isinstance(target, DevTarget):
            raise GameObservationError("DEV_GAME_OBSERVATION_TARGET_INVALID", "Bridge получил некорректный target")
        return self.registry.capture(
            target=target,
            capability_id=capability_id,
            parameters=parameters,
            checkpoint_id=checkpoint_id,
            session_id=session_id,
            smoke_id=smoke_id,
            captured_at=captured_at,
        )


class GameObservationStore:
    """Atomic bounded sidecar под существующим каталoгом конкретного SmokeRun."""

    def __init__(self, environment: object, smoke_id: str) -> None:
        repository_root = getattr(environment, "repository_root", None)
        if not isinstance(repository_root, Path):
            repository_root = Path(repository_root)
        self.repository_root = repository_root.resolve()
        self.smoke_id = _identifier(smoke_id, field="smoke_id")
        self.root = _ensure_scoped_path(
            self.repository_root / "config" / "state" / "dev-runtime-smoke" / self.smoke_id,
            self.repository_root,
            label="каталог game observation",
        )
        self.path = _ensure_scoped_path(
            self.root / "game-observations.json",
            self.repository_root,
            label="файл game observations",
        )
        self.lock_path = _ensure_scoped_path(
            self.root / "game-observations.lock",
            self.repository_root,
            label="блокировка game observations",
        )

    @property
    def relative_file(self) -> str:
        return f"config/state/dev-runtime-smoke/{self.smoke_id}/game-observations.json"

    def _empty(self) -> dict[str, object]:
        return {
            "schema_version": GAME_OBSERVATION_SCHEMA_VERSION,
            "smoke_id": self.smoke_id,
            "observations": [],
        }

    def _read_locked(self) -> list[GameObservationSnapshot]:
        if not self.path.exists():
            return []
        if _is_reparse_point(self.path):
            raise GameObservationError("DEV_GAME_OBSERVATION_UNSAFE_PATH", "Файл game observations не должен быть ссылкой")
        try:
            raw = read_bounded_bytes(self.path, max_bytes=GAME_OBSERVATION_MAX_STORE_BYTES)
            payload = json.loads(raw.decode("utf-8"))
        except FileNotFoundError:
            return []
        except BoundedReadTooLarge as exc:
            raise GameObservationError("DEV_GAME_OBSERVATION_TOO_LARGE", "Файл game observations превышает ограничение") from exc
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise GameObservationError("DEV_GAME_OBSERVATION_CORRUPT", "Файл game observations невозможно прочитать") from exc
        if not isinstance(payload, Mapping) or set(payload) != {"schema_version", "smoke_id", "observations"}:
            raise GameObservationError("DEV_GAME_OBSERVATION_CORRUPT", "Файл game observations имеет неверную схему")
        if (
            type(payload.get("schema_version")) is not int
            or payload.get("schema_version") != GAME_OBSERVATION_SCHEMA_VERSION
            or payload.get("smoke_id") != self.smoke_id
        ):
            raise GameObservationError("DEV_GAME_OBSERVATION_CORRUPT", "Файл game observations имеет неверную привязку")
        raw_items = payload.get("observations")
        if not isinstance(raw_items, list) or len(raw_items) > GAME_OBSERVATION_MAX_SNAPSHOTS:
            raise GameObservationError("DEV_GAME_OBSERVATION_CORRUPT", "Файл game observations имеет неверный размер")
        try:
            items = [GameObservationSnapshot.from_dict(item) for item in raw_items]
        except GameObservationError:
            raise
        except (TypeError, ValueError) as exc:
            raise GameObservationError("DEV_GAME_OBSERVATION_CORRUPT", "Snapshot game observations повреждён") from exc
        seen_keys: set[tuple[str, str]] = set()
        for item in items:
            if item.smoke_id != self.smoke_id:
                raise GameObservationError("DEV_GAME_OBSERVATION_TARGET_MISMATCH", "Snapshot относится к другому SmokeRun")
            key = (item.checkpoint_id, item.capability_id)
            if key in seen_keys:
                raise GameObservationError(
                    "DEV_GAME_OBSERVATION_CORRUPT",
                    "Файл game observations содержит duplicate checkpoint capability",
                )
            seen_keys.add(key)
        return items

    def append(self, snapshot: GameObservationSnapshot, *, duplicate_policy: str = "reject") -> bool:
        if not isinstance(snapshot, GameObservationSnapshot):
            raise TypeError("snapshot должен быть GameObservationSnapshot")
        if snapshot.smoke_id != self.smoke_id:
            raise GameObservationError("DEV_GAME_OBSERVATION_SCOPE_MISMATCH", "Snapshot относится к другому SmokeRun")
        if not isinstance(duplicate_policy, str) or duplicate_policy not in {"reject", "keep_first"}:
            raise GameObservationError("DEV_GAME_CHECKPOINT_POLICY_INVALID", "duplicate policy не поддерживается")
        self.root.parent.mkdir(parents=True, exist_ok=True)
        if _is_reparse_point(self.root) or _is_reparse_point(self.lock_path):
            raise GameObservationError("DEV_GAME_OBSERVATION_UNSAFE_PATH", "Каталог game observations не должен быть ссылкой")
        with _exclusive_policy_lock(self.lock_path):
            items = self._read_locked()
            key = (snapshot.checkpoint_id, snapshot.capability_id)
            if any((item.checkpoint_id, item.capability_id) == key for item in items):
                if duplicate_policy == "keep_first":
                    return False
                raise GameObservationError("DEV_GAME_CHECKPOINT_DUPLICATE", "Checkpoint capability уже сохранён")
            if len(items) >= GAME_OBSERVATION_MAX_SNAPSHOTS:
                raise GameObservationError("DEV_GAME_OBSERVATION_LIMIT", "SmokeRun достиг лимита game observations")
            self.root.mkdir(parents=True, exist_ok=True)
            payload = self._empty()
            payload["observations"] = [item.as_dict() for item in (*items, snapshot)]
            try:
                encoded = json.dumps(
                    payload,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                if len(encoded) > GAME_OBSERVATION_MAX_STORE_BYTES:
                    raise GameObservationError(
                        "DEV_GAME_OBSERVATION_TOO_LARGE",
                        "Файл game observations превышает ограничение",
                    )
                _atomic_json_write(self.path, payload)
            except GameObservationError:
                raise
            except Exception as exc:
                raise GameObservationError("DEV_GAME_OBSERVATION_WRITE_FAILED", "Game observation не сохранён") from exc
        return True

    def read(self, *, checkpoint_id: str | None = None) -> tuple[GameObservationSnapshot, ...]:
        if checkpoint_id is not None:
            checkpoint_id = _identifier(checkpoint_id, field="checkpoint_id")
        if not self.root.exists():
            return ()
        if _is_reparse_point(self.root) or _is_reparse_point(self.lock_path):
            raise GameObservationError("DEV_GAME_OBSERVATION_UNSAFE_PATH", "Каталог game observations не должен быть ссылкой")
        with _exclusive_policy_lock(self.lock_path):
            items = self._read_locked()
        if checkpoint_id is not None:
            items = [item for item in items if item.checkpoint_id == checkpoint_id]
        return tuple(items)

    def summary(self) -> dict[str, object]:
        items = self.read()
        profiles = {item.profile_name for item in items}
        identities = {item.target_identity for item in items}
        return {
            "schema_version": GAME_OBSERVATION_SCHEMA_VERSION,
            "smoke_id": self.smoke_id,
            "count": len(items),
            "checkpoints": sorted({item.checkpoint_id for item in items}),
            "capabilities": sorted({item.capability_id for item in items}),
            "statuses": sorted({item.status.value for item in items}),
            "profile_name": next(iter(profiles)) if len(profiles) == 1 else None,
            "target_identity": next(iter(identities)) if len(identities) == 1 else None,
            "relative_file": self.relative_file,
        }


def build_default_game_observation_registry(
    *,
    game_read_service_factory: Callable[[], GameReadService],
    morale_service_factory: Callable[[], object],
) -> GameObservationRegistry:
    return GameObservationRegistry(
        (
            ResourcesObservationProvider(game_read_service_factory),
            MoraleObservationProvider(morale_service_factory),
        )
    )


def build_runtime_game_bridge(
    environment: object,
    *,
    clock: Callable[[], datetime] | None = None,
) -> DevGameBridge:
    """Собрать bridge на существующих application adapters и persistence root.

    Сборка не создаёт Device и не открывает PostgreSQL connection. Legacy
    generated sources читаются только для typed Stage 9 projection; morale
    factory получает уже общий LazyEngine при первом фактическом observation.
    """

    repository_root = getattr(environment, "repository_root", None)
    if repository_root is None:
        raise TypeError("environment должен содержать repository_root")
    repository_root = Path(repository_root).resolve()

    from module.application.legacy_adapters import (
        GeneratedTaskCatalogAdapter,
        LegacyInstanceRuntimeAdapter,
    )
    from module.application.legacy_game_adapters import (
        LegacyConfigAdapter,
        LegacyRuntimeLogAdapter,
        LegacyScreenshotAdapter,
    )
    from module.application.morale import MoraleService
    from module.persistence.runtime import (
        build_runtime_database_diagnostics,
        runtime_engine,
    )
    from module.persistence.unit_of_work import PostgresUnitOfWork

    metadata = GeneratedTaskCatalogAdapter.from_generated_sources()
    instance_reader = LegacyInstanceRuntimeAdapter()
    game_read_service = GameReadService(
        instance_reader,
        LegacyConfigAdapter(metadata),
        LegacyRuntimeLogAdapter(repository_root / "log"),
        LegacyScreenshotAdapter(),
        metadata,
    )

    def morale_service_factory() -> MoraleService:
        # Диагностический composition root только собирает общий lazy engine:
        # observation не должна мигрировать marker или поднимать runtime service.
        build_runtime_database_diagnostics(environment)
        engine = runtime_engine()
        if engine is None:
            raise RuntimeError("Общий persistence Engine не собран")
        return MoraleService(
            lambda: PostgresUnitOfWork(engine),
            clock=clock,
        )

    return DevGameBridge(
        game_read_service_factory=lambda: game_read_service,
        morale_service_factory=morale_service_factory,
    )


__all__ = [
    "GAME_OBSERVATION_MAX_CAPABILITIES",
    "GAME_OBSERVATION_MAX_PARAMETER_VALUE",
    "GAME_OBSERVATION_MAX_PAYLOAD_BYTES",
    "GAME_OBSERVATION_MAX_SNAPSHOTS",
    "GAME_OBSERVATION_MAX_STORE_BYTES",
    "GAME_OBSERVATION_SCHEMA_VERSION",
    "DevGameBridge",
    "GameObservationCapability",
    "GameObservationCapture",
    "GameObservationError",
    "GameObservationProvider",
    "GameObservationRegistry",
    "GameObservationSnapshot",
    "GameObservationStatus",
    "GameObservationStore",
    "MoraleObservationProvider",
    "ObservationParameter",
    "ObservationParameterType",
    "ResourcesObservationProvider",
    "build_default_game_observation_registry",
    "build_runtime_game_bridge",
]
