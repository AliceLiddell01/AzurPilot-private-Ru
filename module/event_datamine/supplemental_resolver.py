"""Сборка composite EventSpec из ShareCfg и проверенного supplemental snapshot."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any

from module.event_datamine.supplemental import (
    DEFAULT_ASSET_ROOT,
    EVENT_SUPPLEMENTAL_ROOT,
    EventSupplementalError,
    load_supplemental,
    require_mapping,
)
from module.event_datamine.supplemental_verify import (
    apply_resource_display_assets,
    apply_shop_overrides,
    apply_task_classification,
    finding_resource_is_resolved,
    validate_base_contract,
    validate_external_tables,
)

_PARTIAL_FINDING_CODES = frozenset(
    {
        "asset_unresolved",
        "map_pt_amount_unavailable",
        "milestone_missing",
        "pt_source_kind_unknown",
        "shop_activity_missing",
        "source_name_unlocalized",
    }
)


def _build_map_pt_sources(
    spec: dict[str, Any], supplemental: Mapping[str, Any]
) -> None:
    task_sources = [
        item
        for item in spec.get("pt_sources", [])
        if isinstance(item, dict)
        and not (
            str(item.get("id") or "").startswith("map:")
            or str(item.get("id") or "").startswith("map-daily-first-clear:")
        )
    ]
    map_rows = {
        int(item.get("map_id", 0) or 0): item
        for item in supplemental.get("farm", {}).get("maps", [])
        if isinstance(item, Mapping)
    }
    base_maps = [
        item for item in spec.get("maps", []) if isinstance(item, Mapping)
    ]
    base_ids = {int(item.get("id", 0) or 0) for item in base_maps}
    if set(map_rows) != base_ids:
        raise EventSupplementalError(
            "Supplemental farm map inventory не совпадает с EventSpec"
        )

    rebuilt: list[dict[str, Any]] = []
    for base_map in base_maps:
        map_id = int(base_map.get("id", 0) or 0)
        row = map_rows[map_id]
        chapter = str(base_map.get("chapter_name") or "")
        if chapter != str(row.get("chapter_name") or ""):
            raise EventSupplementalError(
                f"Map {map_id} chapter_name не совпадает с supplemental"
            )
        if not bool(row.get("grants_event_pt")):
            continue
        base_points = int(row.get("base_points", 0) or 0)
        multiplier = int(row.get("daily_first_clear_multiplier", 0) or 0)
        daily_limit = int(row.get("daily_limit", 0) or 0)

        if multiplier > 1:
            rebuilt.append(
                {
                    "id": f"map-daily-first-clear:{map_id}",
                    "kind": "daily_first_clear",
                    "name": chapter,
                    "points": base_points * multiplier,
                    "recurring": True,
                    "source_ids": [map_id],
                    "base_points": base_points,
                    "bonus_points": base_points * (multiplier - 1),
                    "multiplier": multiplier,
                    "daily_limit": daily_limit or 1,
                    "includes_base_points": True,
                    "points_source": "supplemental",
                }
            )
        if daily_limit and multiplier <= 1:
            rebuilt.append(
                {
                    "id": f"map-daily-first-clear:{map_id}",
                    "kind": "daily_first_clear",
                    "name": chapter,
                    "points": base_points,
                    "recurring": True,
                    "source_ids": [map_id],
                    "base_points": base_points,
                    "daily_limit": daily_limit,
                    "includes_base_points": True,
                    "points_source": "supplemental",
                }
            )
            continue
        rebuilt.append(
            {
                "id": f"map:{map_id}",
                "kind": "repeatable_map_clear",
                "name": chapter,
                "points": base_points,
                "recurring": False,
                "source_ids": [map_id],
                "base_points": base_points,
                "points_source": "supplemental",
            }
        )
    spec["pt_sources"] = task_sources + rebuilt


def _apply_farm_metadata(
    spec: dict[str, Any], supplemental: Mapping[str, Any]
) -> None:
    farm = copy.deepcopy(dict(require_mapping(supplemental["farm"], "farm")))
    spec["farm"] = farm
    maps = {
        int(item.get("map_id", 0) or 0): item
        for item in farm.get("maps", [])
        if isinstance(item, Mapping)
    }
    for base_map in spec.get("maps", []):
        if not isinstance(base_map, dict):
            continue
        map_id = int(base_map.get("id", 0) or 0)
        meta = maps.get(map_id)
        if meta is None:
            raise EventSupplementalError(f"Map {map_id} отсутствует в farm metadata")
        base_map["supplemental_farm"] = copy.deepcopy(dict(meta))


def _filter_resolved_findings(
    spec: dict[str, Any],
    *,
    fixed_shop_paths: set[str],
    fixed_task_paths: set[str],
    resource_ids: set[int],
) -> None:
    kept: list[dict[str, Any]] = []
    for finding in spec.get("findings", []):
        if not isinstance(finding, Mapping):
            continue
        code = str(finding.get("code") or "")
        path = str(finding.get("path") or "")
        if code == "map_pt_amount_unavailable":
            continue
        if code == "source_name_unlocalized" and path in fixed_shop_paths:
            continue
        if code == "pt_source_kind_unknown" and path in fixed_task_paths:
            continue
        if finding_resource_is_resolved(finding, spec, resource_ids):
            continue
        kept.append(copy.deepcopy(dict(finding)))
    spec["findings"] = kept


def _append_source_conflicts(
    spec: dict[str, Any], supplemental: Mapping[str, Any]
) -> None:
    findings = [
        copy.deepcopy(dict(item))
        for item in spec.get("findings", [])
        if isinstance(item, Mapping)
    ]
    for conflict in supplemental.get("source_conflicts", []):
        if not isinstance(conflict, Mapping):
            continue
        field = str(conflict.get("field") or "unknown")
        note = str(
            conflict.get("resolution")
            or "Supplemental source содержит конфликт значений"
        )
        findings.append(
            {
                "code": "supplemental_source_conflict",
                "severity": "info",
                "message": note,
                "path": f"supplemental.{field}",
                "evidence": {
                    key: value
                    for key, value in conflict.items()
                    if key not in {"field", "resolution"}
                },
            }
        )
    spec["findings"] = findings


def _resolved_status(spec: Mapping[str, Any]) -> str:
    findings = [
        item for item in spec.get("findings", []) if isinstance(item, Mapping)
    ]
    if any(str(item.get("severity") or "") == "error" for item in findings):
        return "unsupported"
    if any(str(item.get("code") or "") in _PARTIAL_FINDING_CODES for item in findings):
        return "partial"
    return "verified"


def resolve_event_spec(
    artifact: Mapping[str, Any],
    *,
    supplemental_root: Path | str = EVENT_SUPPLEMENTAL_ROOT,
    asset_root: Path | str = DEFAULT_ASSET_ROOT,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Вернуть composite EventSpec и метаданные разрешения supplemental-слоя."""

    base_spec = require_mapping(artifact.get("event_spec"), "artifact.event_spec")
    event_id = str(base_spec.get("id") or "")
    supplemental = load_supplemental(event_id, root=supplemental_root)
    if supplemental is None:
        spec = copy.deepcopy(dict(base_spec))
        provenance = (
            spec.get("provenance")
            if isinstance(spec.get("provenance"), dict)
            else {}
        )
        base_revision = str(provenance.get("revision") or "")
        provenance["composite_revision"] = base_revision
        spec["provenance"] = provenance
        return spec, {
            "kind": "sharecfg_only",
            "base_source_status": str(spec.get("source_status") or "unsupported"),
            "resolved_source_status": str(spec.get("source_status") or "unsupported"),
            "base_revision": base_revision,
            "composite_revision": base_revision,
            "supplemental_digest": "",
        }

    validate_base_contract(base_spec, supplemental)
    validate_external_tables(base_spec, supplemental)
    spec = copy.deepcopy(dict(base_spec))
    fixed_shop_paths = apply_shop_overrides(spec, supplemental)
    fixed_task_paths = apply_task_classification(spec, supplemental)
    resource_ids = apply_resource_display_assets(
        spec, supplemental, asset_root=Path(asset_root).resolve()
    )
    _build_map_pt_sources(spec, supplemental)
    _apply_farm_metadata(spec, supplemental)
    _filter_resolved_findings(
        spec,
        fixed_shop_paths=fixed_shop_paths,
        fixed_task_paths=fixed_task_paths,
        resource_ids=resource_ids,
    )
    _append_source_conflicts(spec, supplemental)
    spec["missions"] = copy.deepcopy(list(supplemental.get("missions", [])))
    spec["supplemental_verification"] = copy.deepcopy(
        dict(supplemental.get("verification", {}))
    )
    base_status = str(base_spec.get("source_status") or "unsupported")
    spec["source_status"] = _resolved_status(spec)
    spec["eligible"] = spec["source_status"] != "unsupported" and not any(
        str(item.get("severity") or "") == "error"
        for item in spec.get("findings", [])
        if isinstance(item, Mapping)
    )

    provenance = (
        copy.deepcopy(dict(spec.get("provenance", {})))
        if isinstance(spec.get("provenance"), Mapping)
        else {}
    )
    base_revision = str(provenance.get("revision") or "")
    digest = str(supplemental.get("digest") or "")
    composite_revision = sha256(
        f"{base_revision}\0{digest}".encode("utf-8")
    ).hexdigest()
    provenance["base_revision"] = base_revision
    provenance["source_revision"] = base_revision
    provenance["revision"] = composite_revision
    provenance["composite_revision"] = composite_revision
    provenance["supplemental_digest"] = digest
    provenance["supplemental_provider"] = str(
        supplemental.get("source", {}).get("site") or "pinned supplemental"
    )
    spec["provenance"] = provenance
    spec["supplemental"] = {
        "digest": digest,
        "source": copy.deepcopy(dict(supplemental.get("source", {}))),
    }
    resolution = {
        "kind": "sharecfg_plus_supplemental",
        "base_source_status": base_status,
        "resolved_source_status": spec["source_status"],
        "base_revision": base_revision,
        "composite_revision": composite_revision,
        "supplemental_digest": digest,
    }
    return spec, resolution
