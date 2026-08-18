"""Проверяемый supplemental-слой для фактов события, отсутствующих в ShareCfg."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any

from module.event_datamine.artifact import build_artifact, canonical_json

EVENT_SUPPLEMENTAL_SCHEMA_VERSION = 1
EVENT_SUPPLEMENTAL_ROOT = Path(__file__).with_name("supplemental_data")
DEFAULT_ASSET_ROOT = Path(__file__).resolve().parents[2] / "assets"
_EVENT_ID = re.compile(r"[A-Za-z0-9_.-]+:[1-9][0-9]*")
_PT_KINDS = frozenset(
    {
        "daily",
        "weekly",
        "one_time",
        "first_clear",
        "daily_first_clear",
        "repeatable_map_clear",
        "challenge",
        "unknown",
    }
)


class EventSupplementalError(ValueError):
    """Supplemental-данные не прошли проверку базового Event artifact."""


def supplemental_digest(data: Mapping[str, Any]) -> str:
    clean = dict(data)
    clean.pop("digest", None)
    return sha256(canonical_json(clean).encode("utf-8")).hexdigest()


def event_supplemental_slug(event_id: str) -> str:
    value = str(event_id or "").strip()
    if not _EVENT_ID.fullmatch(value):
        raise EventSupplementalError(f"Некорректная Event identity: {event_id!r}")
    return value.lower().replace(":", "-", 1)


def require_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EventSupplementalError(f"{path} должен быть JSON object")
    return value


def require_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise EventSupplementalError(f"{path} должен быть JSON array")
    return value


def require_int(value: Any, path: str) -> int:
    """Прочитать строгое целое поле без bool и неявного float truncation."""

    if isinstance(value, bool) or isinstance(value, float):
        raise EventSupplementalError(f"{path} должен быть целым числом")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"[+-]?[0-9]+", text):
            return int(text)
    raise EventSupplementalError(f"{path} должен быть целым числом")


def require_positive_json_int(value: Any, path: str) -> int:
    """Проверить runtime-число без строковых и bool coercion."""

    if type(value) is not int or value <= 0:
        raise EventSupplementalError(f"{path} должен быть положительным JSON integer")
    return value


def _unique_ints(items: list[Any], field: str, path: str) -> None:
    values: list[int] = []
    for item in items:
        row = require_mapping(item, path)
        try:
            raw = row[field]
        except KeyError as exc:
            raise EventSupplementalError(
                f"{path}.{field} содержит некорректную identity"
            ) from exc
        try:
            values.append(require_int(raw, f"{path}.{field}"))
        except EventSupplementalError as exc:
            raise EventSupplementalError(
                f"{path}.{field} содержит некорректную identity"
            ) from exc
    if len(values) != len(set(values)):
        raise EventSupplementalError(f"{path}.{field} содержит дубликаты")


def validate_supplemental(data: Any) -> dict[str, Any]:
    raw = require_mapping(data, "supplemental")
    result = copy.deepcopy(dict(raw))
    if (
        require_int(
            result.get("supplemental_schema_version", 0),
            "supplemental_schema_version",
        )
        != EVENT_SUPPLEMENTAL_SCHEMA_VERSION
    ):
        raise EventSupplementalError("Неподдерживаемая версия Event supplemental")
    event_supplemental_slug(str(result.get("event_id") or ""))
    if str(result.get("digest") or "") != supplemental_digest(result):
        raise EventSupplementalError("Digest Event supplemental не совпадает")

    base = require_mapping(result.get("base_contract"), "base_contract")
    required_base = {
        "activity_id",
        "event_name",
        "map_count",
        "milestone_count",
        "server",
        "shop_count",
        "source_revision",
    }
    missing = sorted(required_base - set(base))
    if missing:
        raise EventSupplementalError(
            "base_contract не содержит обязательные поля: " + ", ".join(missing)
        )

    tasks = require_list(result.get("task_classification", []), "task_classification")
    _unique_ints(tasks, "task_id", "task_classification")
    for item in tasks:
        row = require_mapping(item, "task_classification")
        kind = str(row.get("kind") or "")
        if kind not in _PT_KINDS - {"unknown"}:
            raise EventSupplementalError(
                f"task_classification содержит неподдерживаемый kind: {kind!r}"
            )
        if require_int(
            row.get("expected_points", 0), "task_classification.expected_points"
        ) <= 0:
            raise EventSupplementalError(
                "task_classification требует expected_points > 0"
            )
        if not str(row.get("expected_name") or "").strip():
            raise EventSupplementalError("task_classification требует expected_name")

    shop = require_list(result.get("shop_overrides", []), "shop_overrides")
    _unique_ints(shop, "row_id", "shop_overrides")
    resources = require_list(
        result.get("resource_display_assets", []), "resource_display_assets"
    )
    _unique_ints(resources, "resource_id", "resource_display_assets")

    farm = require_mapping(result.get("farm"), "farm")
    maps = require_list(farm.get("maps"), "farm.maps")
    _unique_ints(maps, "map_id", "farm.maps")
    known_chapters: set[str] = set()
    for item in maps:
        row = require_mapping(item, "farm.maps")
        chapter_name = str(row.get("chapter_name") or "").strip()
        if not chapter_name:
            raise EventSupplementalError("farm.maps требует chapter_name")
        known_chapters.add(chapter_name)

    for item in maps:
        row = require_mapping(item, "farm.maps")
        chapter_name = str(row.get("chapter_name") or "").strip()
        grants_pt = row.get("grants_event_pt")
        if not isinstance(grants_pt, bool):
            raise EventSupplementalError("farm.maps.grants_event_pt должен быть bool")
        if grants_pt and require_int(row.get("base_points", 0), "farm.maps.base_points") <= 0:
            raise EventSupplementalError(
                f"farm map {row.get('map_id')} требует положительный base_points"
            )
        if not grants_pt and row.get("base_points") not in (None, 0):
            raise EventSupplementalError(
                f"farm map {row.get('map_id')} не даёт PT, но содержит base_points"
            )

        unlock_requires = require_list(
            row.get("unlock_requires", []),
            f"farm.maps.{chapter_name}.unlock_requires",
        )
        normalized_requires: list[str] = []
        for required in unlock_requires:
            if not isinstance(required, str) or not required.strip():
                raise EventSupplementalError(
                    f"farm.maps.{chapter_name}.unlock_requires должен содержать непустые chapter_name"
                )
            normalized_requires.append(required.strip())
        if len(normalized_requires) != len(set(normalized_requires)):
            raise EventSupplementalError(
                f"farm.maps.{chapter_name}.unlock_requires содержит дубликаты"
            )
        unknown_requires = sorted(set(normalized_requires) - known_chapters)
        if unknown_requires:
            raise EventSupplementalError(
                f"farm.maps.{chapter_name}.unlock_requires ссылается на неизвестные карты: "
                + ", ".join(unknown_requires)
            )

        if "daily_first_clear_multiplier" in row:
            require_positive_json_int(
                row["daily_first_clear_multiplier"],
                f"farm.maps.{chapter_name}.daily_first_clear_multiplier",
            )
        if "daily_limit" in row:
            require_positive_json_int(
                row["daily_limit"],
                f"farm.maps.{chapter_name}.daily_limit",
            )
        if "oil" in row:
            oil = require_mapping(row["oil"], f"farm.maps.{chapter_name}.oil")
            if "per_run" in oil:
                require_positive_json_int(
                    oil["per_run"],
                    f"farm.maps.{chapter_name}.oil.per_run",
                )

    verification = require_mapping(result.get("verification"), "verification")
    require_mapping(verification.get("shop"), "verification.shop")
    require_mapping(verification.get("milestones"), "verification.milestones")
    return result


def _safe_part_name(value: Any) -> str:
    name = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.json", name):
        raise EventSupplementalError(f"Некорректное имя supplemental part: {name!r}")
    return name


def load_supplemental(
    event_id: str,
    *,
    root: Path | str = EVENT_SUPPLEMENTAL_ROOT,
) -> dict[str, Any] | None:
    directory = Path(root) / event_supplemental_slug(event_id)
    manifest_path = directory / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        result = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = require_mapping(result, "supplemental manifest")
        map_parts = require_list(manifest.get("map_parts", []), "map_parts")
        maps: list[Any] = []
        for raw_name in map_parts:
            name = _safe_part_name(raw_name)
            part_path = (directory / name).resolve()
            if directory.resolve() not in part_path.parents:
                raise EventSupplementalError("Supplemental part вышел за пределы каталога")
            part = json.loads(part_path.read_text(encoding="utf-8"))
            maps.extend(require_list(part, f"map part {name}"))
    except EventSupplementalError:
        raise
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EventSupplementalError(
            f"Не удалось прочитать Event supplemental {manifest_path}"
        ) from exc

    assembled = copy.deepcopy(dict(manifest))
    farm = copy.deepcopy(dict(require_mapping(assembled.get("farm"), "farm")))
    farm["maps"] = maps
    assembled["farm"] = farm
    return validate_supplemental(assembled)


def resolve_supplemental_event_spec(
    artifact: Mapping[str, Any],
    *,
    supplemental_root: Path | str = EVENT_SUPPLEMENTAL_ROOT,
    asset_root: Path | str = DEFAULT_ASSET_ROOT,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Вернуть runtime composite EventSpec, не изменяя базовый signed artifact."""

    from module.event_datamine.supplemental_resolver import resolve_event_spec

    return resolve_event_spec(
        artifact,
        supplemental_root=supplemental_root,
        asset_root=asset_root,
    )


def resolve_supplemental_artifact(
    artifact: Mapping[str, Any],
    *,
    supplemental_root: Path | str = EVENT_SUPPLEMENTAL_ROOT,
    asset_root: Path | str = DEFAULT_ASSET_ROOT,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Построить валидный runtime artifact, сохранив raw artifact неизменным.

    Некорректный или устаревший supplemental не уничтожает доказанный ShareCfg:
    runtime получает исходный EventSpec с явным warning и исходной revision.
    """

    base_spec = require_mapping(artifact.get("event_spec"), "artifact.event_spec")
    try:
        spec, resolution = resolve_supplemental_event_spec(
            artifact,
            supplemental_root=supplemental_root,
            asset_root=asset_root,
        )
    except (TypeError, ValueError, KeyError, OverflowError) as exc:
        spec = copy.deepcopy(dict(base_spec))
        findings = [
            copy.deepcopy(dict(item))
            for item in spec.get("findings", [])
            if isinstance(item, Mapping)
        ]
        findings.append(
            {
                "code": "supplemental_rejected",
                "severity": "warning",
                "message": str(exc),
                "path": "supplemental",
            }
        )
        spec["findings"] = findings
        if str(spec.get("source_status") or "unsupported") == "verified":
            spec["source_status"] = "partial"
        spec["eligible"] = spec.get("source_status") != "unsupported" and not any(
            str(item.get("severity") or "") == "error" for item in findings
        )
        provenance = (
            copy.deepcopy(dict(spec.get("provenance", {})))
            if isinstance(spec.get("provenance"), Mapping)
            else {}
        )
        base_revision = str(provenance.get("revision") or "")
        provenance["composite_revision"] = base_revision
        spec["provenance"] = provenance
        resolution = {
            "kind": "supplemental_rejected",
            "base_source_status": str(base_spec.get("source_status") or "unsupported"),
            "resolved_source_status": str(spec.get("source_status") or "unsupported"),
            "base_revision": base_revision,
            "composite_revision": base_revision,
            "supplemental_digest": "",
            "error": str(exc),
        }

    if resolution["kind"] == "sharecfg_only":
        return copy.deepcopy(dict(artifact)), resolution

    metadata = (
        copy.deepcopy(dict(artifact.get("metadata", {})))
        if isinstance(artifact.get("metadata"), Mapping)
        else {}
    )
    metadata["runtime_resolution"] = copy.deepcopy(resolution)
    runtime_artifact = build_artifact(
        spec,
        compiler_version=str(artifact.get("compiler_version") or "1"),
        role=str(artifact.get("role") or "production"),
        metadata=metadata,
    )
    return runtime_artifact, resolution


def enrich_event_plan_with_supplemental(
    plan: Mapping[str, Any],
    spec: Mapping[str, Any],
) -> dict[str, Any]:
    """Спроецировать static supplemental-факты без подмены runtime evidence."""

    from module.event_datamine.supplemental_projection import enrich_event_plan

    return enrich_event_plan(plan, spec)
