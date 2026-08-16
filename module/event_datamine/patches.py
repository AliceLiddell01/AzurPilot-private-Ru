"""Проверяемые структурные исключения компилятора Event-карт.

Конкретные события и карты описываются JSON-данными в ``compatibility_data``.
Этот модуль знает только схему, проверяет evidence и возвращает типизированные
исключения без ветвлений по identity события.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any

from module.event_datamine.artifact import canonical_json

COMPATIBILITY_SCHEMA_VERSION = 1
COMPATIBILITY_ROOT = Path(__file__).with_name("compatibility_data")
_EVENT_ID = re.compile(r"[a-z0-9_]+:[0-9]+")
_SHA40 = re.compile(r"[0-9a-f]{40}")
_SAFE_SLUG = re.compile(r"[a-z0-9_-]+")
_ALLOWED_TOP_LEVEL = {
    "compatibility_schema_version",
    "digest",
    "event_id",
    "evidence",
    "patches",
}
_ALLOWED_EVIDENCE = {"repository", "revision"}
_ALLOWED_PATCH = {
    "id",
    "map_id",
    "ignored_land_rotations",
    "reason",
    "source_path",
}


class CompatibilityDataError(ValueError):
    """Данные структурной совместимости не прошли проверку."""


@dataclass(frozen=True)
class CompatibilityPatch:
    id: str
    event_id: str
    map_id: int
    ignored_land_rotations: tuple[int, ...]
    reason: str
    repository: str
    revision: str
    source_path: str

    @property
    def source_evidence(self) -> str:
        return f"{self.repository}@{self.revision} {self.source_path}"

    @property
    def expected_effect(self) -> str:
        codes = ", ".join(f"code {item}" for item in self.ignored_land_rotations)
        return f"Игнорировать только {codes}; иные неизвестные коды остаются blocking"


def compatibility_digest(data: dict[str, Any]) -> str:
    from hashlib import sha256

    clean = dict(data)
    clean.pop("digest", None)
    return sha256(canonical_json(clean).encode("utf-8")).hexdigest()


def _event_slug(event_id: str) -> str:
    if not _EVENT_ID.fullmatch(event_id):
        raise CompatibilityDataError(f"Некорректный Event identity: {event_id!r}")
    slug = event_id.replace(":", "-")
    if not _SAFE_SLUG.fullmatch(slug):
        raise CompatibilityDataError(f"Некорректный compatibility slug: {slug!r}")
    return slug


def _safe_source_path(value: Any) -> str:
    source_path = str(value or "").strip()
    path = PurePosixPath(source_path)
    if (
        not source_path
        or path.is_absolute()
        or ".." in path.parts
        or path.suffix != ".py"
    ):
        raise CompatibilityDataError(
            f"Некорректный source_path compatibility evidence: {source_path!r}"
        )
    return source_path


def validate_compatibility_data(data: Any, *, event_id: str) -> tuple[CompatibilityPatch, ...]:
    if not isinstance(data, dict):
        raise CompatibilityDataError("Compatibility snapshot должен быть JSON object")
    unknown = set(data) - _ALLOWED_TOP_LEVEL
    if unknown:
        raise CompatibilityDataError(
            f"Неизвестные поля compatibility snapshot: {sorted(unknown)}"
        )
    version = data.get("compatibility_schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise CompatibilityDataError("compatibility_schema_version должен быть int")
    if version != COMPATIBILITY_SCHEMA_VERSION:
        raise CompatibilityDataError("Неподдерживаемая версия compatibility snapshot")
    if str(data.get("event_id") or "") != event_id:
        raise CompatibilityDataError("Compatibility snapshot не соответствует Event identity")
    if str(data.get("digest") or "") != compatibility_digest(data):
        raise CompatibilityDataError("Digest compatibility snapshot не совпадает")

    evidence = data.get("evidence")
    if not isinstance(evidence, dict):
        raise CompatibilityDataError("Compatibility snapshot требует evidence")
    unknown_evidence = set(evidence) - _ALLOWED_EVIDENCE
    if unknown_evidence:
        raise CompatibilityDataError(
            f"Неизвестные поля compatibility evidence: {sorted(unknown_evidence)}"
        )
    repository = str(evidence.get("repository") or "").strip()
    revision = str(evidence.get("revision") or "").lower()
    if not repository or "/" not in repository:
        raise CompatibilityDataError("Compatibility evidence требует repository")
    if not _SHA40.fullmatch(revision):
        raise CompatibilityDataError("Compatibility evidence содержит некорректный Git SHA")

    raw_patches = data.get("patches")
    if not isinstance(raw_patches, list):
        raise CompatibilityDataError("Compatibility snapshot требует список patches")
    result: list[CompatibilityPatch] = []
    ids: set[str] = set()
    keys: set[tuple[int, int]] = set()
    for raw in raw_patches:
        if not isinstance(raw, dict):
            raise CompatibilityDataError("Compatibility patch должен быть JSON object")
        unknown_patch = set(raw) - _ALLOWED_PATCH
        if unknown_patch:
            raise CompatibilityDataError(
                f"Неизвестные поля compatibility patch: {sorted(unknown_patch)}"
            )
        patch_id = str(raw.get("id") or "").strip()
        if not patch_id or patch_id in ids:
            raise CompatibilityDataError(f"Неуникальный compatibility patch id: {patch_id!r}")
        map_id = raw.get("map_id")
        if isinstance(map_id, bool) or not isinstance(map_id, int) or map_id <= 0:
            raise CompatibilityDataError(f"Некорректный map_id compatibility patch: {map_id!r}")
        rotations = raw.get("ignored_land_rotations")
        if not isinstance(rotations, list) or not rotations:
            raise CompatibilityDataError(
                f"Patch {patch_id!r} не содержит ignored_land_rotations"
            )
        normalized: list[int] = []
        for value in rotations:
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise CompatibilityDataError(
                    f"Patch {patch_id!r} содержит некорректный land rotation"
                )
            if value in normalized:
                raise CompatibilityDataError(
                    f"Patch {patch_id!r} содержит дублирующий land rotation {value}"
                )
            normalized.append(value)
            key = (map_id, value)
            if key in keys:
                raise CompatibilityDataError(
                    f"Land rotation {value} карты {map_id} описан более одного раза"
                )
            keys.add(key)
        reason = str(raw.get("reason") or "").strip()
        if not reason:
            raise CompatibilityDataError(f"Patch {patch_id!r} не содержит reason")
        result.append(
            CompatibilityPatch(
                id=patch_id,
                event_id=event_id,
                map_id=map_id,
                ignored_land_rotations=tuple(normalized),
                reason=reason,
                repository=repository,
                revision=revision,
                source_path=_safe_source_path(raw.get("source_path")),
            )
        )
        ids.add(patch_id)
    return tuple(result)


@lru_cache(maxsize=32)
def _load_default(event_id: str) -> tuple[CompatibilityPatch, ...]:
    return load_compatibility_data(event_id)


def load_compatibility_data(
    event_id: str,
    *,
    root: Path | str = COMPATIBILITY_ROOT,
) -> tuple[CompatibilityPatch, ...]:
    slug = _event_slug(event_id)
    base = Path(root).resolve()
    target = (base / f"{slug}.json").resolve()
    if base != target.parent:
        raise CompatibilityDataError("Compatibility path вышел за пределы data root")
    if not target.is_file():
        return ()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CompatibilityDataError(
            f"Не удалось прочитать compatibility snapshot {target}"
        ) from exc
    return validate_compatibility_data(data, event_id=event_id)


def patches_for(event_id: str, map_id: int) -> tuple[CompatibilityPatch, ...]:
    """Вернуть проверенные структурные исключения конкретной карты."""

    return tuple(item for item in _load_default(event_id) if item.map_id == map_id)
