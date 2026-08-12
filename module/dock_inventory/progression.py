"""Offline progression metadata and fail-closed Dock progression derivation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from module.dock_inventory.model import (
    CanonicalShipIdentity,
    IdentityStatus,
    StarObservation,
)

PROGRESSION_SCHEMA_VERSION = 1
DEFAULT_PROGRESSION_CATALOG_PATH = (
    Path(__file__).parents[2] / "assets" / "ship" / "dock_progression_catalog.json"
)
_CANONICAL_ID_RE = re.compile(r"^azur_lane_ship_group:[1-9][0-9]*$")


class ProgressionKind(Enum):
    STANDARD_LIMIT_BREAK = "standard_limit_break"
    NONSTANDARD = "nonstandard"
    UNKNOWN = "unknown"


class ProgressionStatus(Enum):
    KNOWN = "known"
    UNKNOWN = "unknown"


class DockProgressionCatalogError(RuntimeError):
    """The tracked progression catalog is absent or violates its schema."""


@dataclass(frozen=True, slots=True)
class DockProgressionState:
    semantic_id: str
    kind: ProgressionKind
    filled: int
    total: int
    stage_index: int | None = None
    stage_count: int | None = None
    is_max: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.semantic_id, str) or not self.semantic_id:
            raise DockProgressionCatalogError(
                "semantic_id должен быть непустой строкой."
            )
        if self.kind is ProgressionKind.UNKNOWN:
            raise DockProgressionCatalogError(
                "Catalog state не может иметь kind UNKNOWN."
            )
        for name, value in (("filled", self.filled), ("total", self.total)):
            if type(value) is not int or value < 0:
                raise DockProgressionCatalogError(
                    f"{name} должен быть неотрицательным int."
                )
        if self.filled > self.total:
            raise DockProgressionCatalogError("filled не может превышать total.")
        if type(self.is_max) is not bool:
            raise DockProgressionCatalogError("is_max должен быть bool.")
        if self.kind is ProgressionKind.STANDARD_LIMIT_BREAK:
            if (
                type(self.stage_index) is not int
                or type(self.stage_count) is not int
                or self.stage_count < 1
                or not 0 <= self.stage_index < self.stage_count
            ):
                raise DockProgressionCatalogError(
                    "Standard limit-break state требует корректные stage_index/stage_count."
                )
            if self.is_max != (self.stage_index == self.stage_count - 1):
                raise DockProgressionCatalogError(
                    "is_max standard state должен соответствовать последнему stage."
                )
        elif self.stage_index is not None or self.stage_count is not None:
            raise DockProgressionCatalogError(
                "Nonstandard state не должен притворяться ordinary limit-break stage."
            )


@dataclass(frozen=True, slots=True)
class DockProgressionFamily:
    canonical_id: str
    family_type: str
    states: tuple[DockProgressionState, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.canonical_id, str) or not _CANONICAL_ID_RE.fullmatch(
            self.canonical_id
        ):
            raise DockProgressionCatalogError(
                f"Недопустимый progression canonical_id: {self.canonical_id!r}."
            )
        if not isinstance(self.family_type, str) or not self.family_type:
            raise DockProgressionCatalogError(
                "family_type должен быть непустой строкой."
            )
        if not isinstance(self.states, tuple) or not self.states:
            raise DockProgressionCatalogError(
                "Progression family должна содержать states."
            )
        if not all(isinstance(state, DockProgressionState) for state in self.states):
            raise DockProgressionCatalogError(
                "Progression family содержит неверный state."
            )
        semantic_ids = tuple(state.semantic_id for state in self.states)
        if len(semantic_ids) != len(set(semantic_ids)):
            raise DockProgressionCatalogError(
                "semantic_id должны быть уникальны в family."
            )


@dataclass(frozen=True, slots=True)
class DockProgressionProvenance:
    source_repository: str
    source_commit: str
    source_path: str
    source_blob_sha: str
    source_sha256: str
    supplemental_source_repository: str
    supplemental_source_commit: str
    supplemental_template_path: str
    supplemental_template_blob_sha: str
    blueprint_source_path: str
    blueprint_source_blob_sha: str
    level_source_path: str
    level_source_blob_sha: str
    selection_contract: str

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise DockProgressionCatalogError(
                    "Все поля progression provenance должны быть непустыми строками."
                )
        if not re.fullmatch(r"[0-9a-f]{40}", self.source_commit):
            raise DockProgressionCatalogError("source_commit должен быть полным SHA-1.")
        if not re.fullmatch(r"[0-9a-f]{40}", self.supplemental_source_commit):
            raise DockProgressionCatalogError(
                "supplemental_source_commit должен быть полным SHA-1."
            )
        for name in (
            "source_blob_sha",
            "supplemental_template_blob_sha",
            "blueprint_source_blob_sha",
            "level_source_blob_sha",
        ):
            if not re.fullmatch(r"[0-9a-f]{40}", getattr(self, name)):
                raise DockProgressionCatalogError(f"{name} должен быть Git blob SHA-1.")
        if not re.fullmatch(r"[0-9a-f]{64}", self.source_sha256):
            raise DockProgressionCatalogError("source_sha256 должен быть SHA-256.")


@dataclass(frozen=True, slots=True)
class DockProgressionCatalog:
    records: tuple[DockProgressionFamily, ...]
    provenance: DockProgressionProvenance
    identity_fingerprint: str
    maximum_observed_level: int
    schema_version: int = PROGRESSION_SCHEMA_VERSION
    identity_scheme: str = "azur_lane_ship_group"
    _by_canonical_id: dict[str, DockProgressionFamily] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if self.schema_version != PROGRESSION_SCHEMA_VERSION:
            raise DockProgressionCatalogError("Неподдерживаемая progression schema.")
        if self.identity_scheme != "azur_lane_ship_group":
            raise DockProgressionCatalogError("Неподдерживаемая identity scheme.")
        if not re.fullmatch(r"[0-9a-f]{64}", self.identity_fingerprint):
            raise DockProgressionCatalogError(
                "identity_fingerprint должен быть SHA-256."
            )
        if (
            type(self.maximum_observed_level) is not int
            or self.maximum_observed_level < 1
        ):
            raise DockProgressionCatalogError(
                "maximum_observed_level должен быть положительным int."
            )
        if not isinstance(self.records, tuple) or not self.records:
            raise DockProgressionCatalogError(
                "Progression records должны быть непустыми."
            )
        identifiers = tuple(record.canonical_id for record in self.records)
        if len(identifiers) != len(set(identifiers)):
            raise DockProgressionCatalogError("Progression catalog содержит дубликаты.")
        if identifiers != tuple(sorted(identifiers, key=_canonical_id_sort_key)):
            raise DockProgressionCatalogError(
                "Progression records должны быть отсортированы по canonical group."
            )
        object.__setattr__(
            self,
            "_by_canonical_id",
            {record.canonical_id: record for record in self.records},
        )

    @property
    def fingerprint(self) -> str:
        semantic = {
            "schema_version": self.schema_version,
            "identity_scheme": self.identity_scheme,
            "maximum_observed_level": self.maximum_observed_level,
            "records": [_family_to_mapping(record) for record in self.records],
        }
        encoded = json.dumps(
            semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def family_for(
        self, identity: CanonicalShipIdentity
    ) -> DockProgressionFamily | None:
        if not isinstance(identity, CanonicalShipIdentity):
            raise TypeError("identity должен быть CanonicalShipIdentity")
        return self._by_canonical_id.get(identity.key)

    @classmethod
    def from_mapping(cls, payload: object) -> DockProgressionCatalog:
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "identity_scheme",
            "identity_fingerprint",
            "maximum_observed_level",
            "provenance",
            "records",
        }:
            raise DockProgressionCatalogError(
                "Progression catalog top-level schema не совпадает с contract."
            )
        raw_provenance = payload["provenance"]
        if not isinstance(raw_provenance, dict) or set(raw_provenance) != set(
            DockProgressionProvenance.__dataclass_fields__
        ):
            raise DockProgressionCatalogError("Progression provenance schema неверна.")
        raw_records = payload["records"]
        if not isinstance(raw_records, list):
            raise DockProgressionCatalogError("Progression records должен быть array.")
        records = []
        for raw_family in raw_records:
            if not isinstance(raw_family, dict) or set(raw_family) != {
                "canonical_id",
                "family_type",
                "states",
            }:
                raise DockProgressionCatalogError("Progression family schema неверна.")
            raw_states = raw_family["states"]
            if not isinstance(raw_states, list):
                raise DockProgressionCatalogError(
                    "Progression states должен быть array."
                )
            states = []
            for raw_state in raw_states:
                if not isinstance(raw_state, dict) or set(raw_state) != {
                    "semantic_id",
                    "kind",
                    "filled",
                    "total",
                    "stage_index",
                    "stage_count",
                    "is_max",
                }:
                    raise DockProgressionCatalogError(
                        "Progression state schema неверна."
                    )
                try:
                    kind = ProgressionKind(raw_state["kind"])
                except (TypeError, ValueError) as exc:
                    raise DockProgressionCatalogError(
                        "Progression kind неизвестен."
                    ) from exc
                states.append(
                    DockProgressionState(
                        semantic_id=raw_state["semantic_id"],
                        kind=kind,
                        filled=raw_state["filled"],
                        total=raw_state["total"],
                        stage_index=raw_state["stage_index"],
                        stage_count=raw_state["stage_count"],
                        is_max=raw_state["is_max"],
                    )
                )
            records.append(
                DockProgressionFamily(
                    canonical_id=raw_family["canonical_id"],
                    family_type=raw_family["family_type"],
                    states=tuple(states),
                )
            )
        return cls(
            schema_version=payload["schema_version"],
            identity_scheme=payload["identity_scheme"],
            identity_fingerprint=payload["identity_fingerprint"],
            maximum_observed_level=payload["maximum_observed_level"],
            provenance=DockProgressionProvenance(**raw_provenance),
            records=tuple(records),
        )


@dataclass(frozen=True, slots=True)
class DockProgressionObservation:
    status: ProgressionStatus
    kind: ProgressionKind
    observed_stars: StarObservation | None
    stage_index: int | None = None
    stage_count: int | None = None
    is_max: bool | None = None
    reason: str | None = None
    matching_semantic_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.status, ProgressionStatus):
            raise TypeError("status должен быть ProgressionStatus")
        if not isinstance(self.kind, ProgressionKind):
            raise TypeError("kind должен быть ProgressionKind")
        if self.observed_stars is not None and not isinstance(
            self.observed_stars, StarObservation
        ):
            raise TypeError("observed_stars должен быть StarObservation или None")
        if not isinstance(self.matching_semantic_ids, tuple) or any(
            not isinstance(value, str) for value in self.matching_semantic_ids
        ):
            raise TypeError("matching_semantic_ids должен быть tuple строк")
        if self.status is ProgressionStatus.KNOWN:
            if (
                self.kind is ProgressionKind.UNKNOWN
                or len(self.matching_semantic_ids) != 1
            ):
                raise ValueError("KNOWN progression требует ровно один semantic state.")
            if self.reason is not None or self.is_max is None:
                raise ValueError(
                    "KNOWN progression не должен содержать unknown reason."
                )
        else:
            if self.kind is not ProgressionKind.UNKNOWN:
                raise ValueError("UNKNOWN progression должен иметь kind UNKNOWN.")
            if not isinstance(self.reason, str) or not self.reason:
                raise ValueError("UNKNOWN progression требует reason.")
            if (
                self.stage_index is not None
                or self.stage_count is not None
                or self.is_max is not None
            ):
                raise ValueError(
                    "UNKNOWN progression не должен содержать guessed stage."
                )


def derive_dock_progression(
    *,
    identity_status: IdentityStatus,
    canonical_identity: CanonicalShipIdentity | None,
    observed_stars: StarObservation | None,
    catalog: DockProgressionCatalog,
) -> DockProgressionObservation:
    """Derive progression only from one unique identity plus raw visual stars."""

    if not isinstance(identity_status, IdentityStatus):
        raise TypeError("identity_status должен быть IdentityStatus")
    if not isinstance(catalog, DockProgressionCatalog):
        raise TypeError("catalog должен быть DockProgressionCatalog")
    if observed_stars is None:
        return _unknown_progression(None, "star_evidence_unknown")
    if not isinstance(observed_stars, StarObservation):
        raise TypeError("observed_stars должен быть StarObservation или None")
    if identity_status is not IdentityStatus.MATCHED or canonical_identity is None:
        return _unknown_progression(observed_stars, "identity_not_unique")
    family = catalog.family_for(canonical_identity)
    if family is None:
        return _unknown_progression(observed_stars, "canonical_family_missing")
    matches = tuple(
        state
        for state in family.states
        if state.filled == observed_stars.filled and state.total == observed_stars.total
    )
    if not matches:
        return _unknown_progression(observed_stars, "visual_static_conflict")
    if len(matches) > 1:
        return _unknown_progression(
            observed_stars,
            "ambiguous_static_mapping",
            matching_semantic_ids=tuple(state.semantic_id for state in matches),
        )
    state = matches[0]
    return DockProgressionObservation(
        status=ProgressionStatus.KNOWN,
        kind=state.kind,
        observed_stars=observed_stars,
        stage_index=state.stage_index,
        stage_count=state.stage_count,
        is_max=state.is_max,
        matching_semantic_ids=(state.semantic_id,),
    )


def _unknown_progression(
    stars: StarObservation | None,
    reason: str,
    *,
    matching_semantic_ids: tuple[str, ...] = (),
) -> DockProgressionObservation:
    return DockProgressionObservation(
        status=ProgressionStatus.UNKNOWN,
        kind=ProgressionKind.UNKNOWN,
        observed_stars=stars,
        reason=reason,
        matching_semantic_ids=matching_semantic_ids,
    )


def _canonical_id_sort_key(value: str) -> int:
    try:
        return int(value.rsplit(":", 1)[1])
    except (IndexError, ValueError) as exc:
        raise DockProgressionCatalogError(
            f"Недопустимый canonical_id: {value!r}."
        ) from exc


def _state_to_mapping(state: DockProgressionState) -> dict[str, object]:
    return {
        "semantic_id": state.semantic_id,
        "kind": state.kind.value,
        "filled": state.filled,
        "total": state.total,
        "stage_index": state.stage_index,
        "stage_count": state.stage_count,
        "is_max": state.is_max,
    }


def _family_to_mapping(family: DockProgressionFamily) -> dict[str, object]:
    return {
        "canonical_id": family.canonical_id,
        "family_type": family.family_type,
        "states": [_state_to_mapping(state) for state in family.states],
    }


def load_dock_progression_catalog(
    path: Path | str = DEFAULT_PROGRESSION_CATALOG_PATH,
) -> DockProgressionCatalog:
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DockProgressionCatalogError(
            f"Progression catalog не найден: {source}."
        ) from exc
    except OSError as exc:
        raise DockProgressionCatalogError(
            f"Progression catalog не удалось прочитать: {source}."
        ) from exc
    except json.JSONDecodeError as exc:
        raise DockProgressionCatalogError(
            f"Progression catalog содержит неверный JSON: {source}."
        ) from exc
    return DockProgressionCatalog.from_mapping(payload)


__all__ = [
    "DockProgressionCatalog",
    "DockProgressionCatalogError",
    "DockProgressionFamily",
    "DockProgressionObservation",
    "DockProgressionProvenance",
    "DockProgressionState",
    "ProgressionKind",
    "ProgressionStatus",
    "derive_dock_progression",
    "load_dock_progression_catalog",
]
