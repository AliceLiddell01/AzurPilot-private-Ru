"""Общие data-driven helpers для Event fixture-контрактов."""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from module.event_datamine.artifact import BUILTIN_ARTIFACT_ROOT
from module.event_datamine.campaign_selector import generated_campaign_package_parts
from module.event_datamine.registry import EventArtifactRegistry
from module.event_datamine.supplemental import (
    event_supplemental_slug,
    supplemental_digest,
)

ROOT = Path(__file__).resolve().parents[1]
CURRENT_FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "event_datamine" / "current_en"
UPSTREAM_RUNTIME_AUDIT_ROOT = (
    ROOT / "tests" / "fixtures" / "event_datamine" / "upstream_runtime_semantics"
)


def current_fixture_manifest() -> dict[str, Any]:
    return json.loads((CURRENT_FIXTURE_ROOT / "manifest.json").read_text(encoding="utf-8"))


def current_fixture_identity() -> tuple[str, str, str, str, int]:
    manifest = current_fixture_manifest()
    source = manifest["source"]
    event_id = str(manifest["event_id"])
    server, raw_activity_id = event_id.split(":", 1)
    return (
        event_id,
        str(source["server"] or server).upper(),
        str(source["repository"]),
        str(source["revision"]),
        int(raw_activity_id),
    )


def production_registry_entry() -> tuple[EventArtifactRegistry, dict[str, Any]]:
    event_id, *_ = current_fixture_identity()
    registry = EventArtifactRegistry()
    matches = [
        entry
        for entry in registry.entries
        if entry["role"] == "production" and entry["id"] == event_id
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"Fixture {event_id!r} должна соответствовать одному production artifact"
        )
    return registry, matches[0]


def production_artifact() -> dict[str, Any]:
    return production_registry_entry()[1]["artifact"]


def production_artifact_path() -> Path:
    registry, entry = production_registry_entry()
    return registry.root / entry["path"]


def artifact_active_time(artifact: dict[str, Any] | None = None) -> datetime:
    source = production_artifact() if artifact is None else artifact
    spec = source["event_spec"]
    start = datetime.fromisoformat(str(spec["farm_start"]))
    end = datetime.fromisoformat(str(spec["farm_end"]))
    if end < start:
        raise AssertionError("Event artifact содержит обратный lifecycle interval")
    return start + (end - start) / 2


def current_generated_package_parts() -> tuple[str, ...]:
    return generated_campaign_package_parts(production_artifact())


def current_generated_package_root() -> Path:
    return ROOT / "campaign" / "generated_event" / Path(*current_generated_package_parts())


def load_upstream_runtime_audits() -> tuple[dict[str, Any], ...]:
    audits = tuple(
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(UPSTREAM_RUNTIME_AUDIT_ROOT.glob("*.json"))
    )
    if not audits:
        raise AssertionError("Не найдены pinned upstream runtime audit fixtures")
    return audits


def write_split_supplemental(root: Path, data: dict[str, Any]) -> Path:
    event_root = root / event_supplemental_slug(str(data["event_id"]))
    event_root.mkdir(parents=True, exist_ok=True)

    prepared = copy.deepcopy(data)
    maps = list(prepared["farm"].pop("maps"))
    parts = list(prepared["map_parts"])
    if not parts:
        raise AssertionError("Supplemental fixture не содержит map_parts")
    if len(maps) < len(parts):
        raise AssertionError("Число supplemental map_parts превышает число карт")

    prepared["digest"] = supplemental_digest(data)
    (event_root / "manifest.json").write_text(
        json.dumps(prepared, ensure_ascii=False),
        encoding="utf-8",
    )

    base_size, remainder = divmod(len(maps), len(parts))
    offset = 0
    for index, name in enumerate(parts):
        size = base_size + (1 if index < remainder else 0)
        rows = maps[offset : offset + size]
        offset += size
        (event_root / name).write_text(
            json.dumps(rows, ensure_ascii=False),
            encoding="utf-8",
        )

    if offset != len(maps):
        raise AssertionError("Не все supplemental maps распределены по map_parts")
    return event_root
