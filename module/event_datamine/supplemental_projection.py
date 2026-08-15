"""Проекция supplemental-фактов в EventPlan без подмены runtime evidence."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any


def enrich_event_plan(
    plan: Mapping[str, Any],
    spec: Mapping[str, Any],
) -> dict[str, Any]:
    result = copy.deepcopy(dict(plan))
    supplemental = spec.get("supplemental")
    if not isinstance(supplemental, Mapping):
        return result

    source_by_id = {
        str(item.get("id") or ""): item
        for item in spec.get("pt_sources", [])
        if isinstance(item, Mapping)
    }
    for item in result.get("pt_sources", []):
        if not isinstance(item, dict):
            continue
        source = source_by_id.get(str(item.get("id") or ""))
        if not isinstance(source, Mapping):
            continue
        for field in (
            "base_points",
            "bonus_points",
            "multiplier",
            "daily_limit",
            "includes_base_points",
            "points_source",
            "classification_source",
            "scope",
        ):
            if field in source:
                item[field] = copy.deepcopy(source[field])

    farm = spec.get("farm")
    if isinstance(farm, Mapping):
        result["farm"] = copy.deepcopy(dict(farm))
        by_map = {
            str(item.get("map_id") or ""): item
            for item in farm.get("maps", [])
            if isinstance(item, Mapping)
        }
        for stage in result.get("stages", []):
            if not isinstance(stage, dict):
                continue
            meta = by_map.get(str(stage.get("id") or ""))
            if not isinstance(meta, Mapping):
                continue
            stage["static_source"] = "supplemental"
            for field in (
                "mode",
                "title",
                "description",
                "grants_event_pt",
                "base_points",
                "daily_first_clear_multiplier",
                "daily_limit",
                "specialized_core_drops",
                "boss_only_ship_drops",
                "unlock_requires",
                "clear_rewards",
                "three_star_rewards",
                "mob_level",
                "siren_level",
                "boss_level",
                "boss_name",
                "required_battles",
                "boss_kills_to_clear",
                "star_conditions",
                "airspace_control_actual",
                "fleet_restrictions",
                "stat_restrictions",
                "map_drop_families",
                "oil",
                "coins",
                "score_counts_toward_ranking",
            ):
                if field in meta:
                    stage[field] = copy.deepcopy(meta[field])
            if stage.get("points") is None and bool(meta.get("grants_event_pt")):
                stage["points"] = int(meta.get("base_points", 0) or 0) or None
                stage["points_source"] = "supplemental"
            oil = meta.get("oil")
            if stage.get("oil") is None and isinstance(oil, Mapping):
                per_run = int(oil.get("per_run", 0) or 0)
                if per_run > 0:
                    stage["oil"] = per_run
                    stage["oil_source"] = "supplemental"

    missions = spec.get("missions")
    if isinstance(missions, list):
        result["missions"] = [
            copy.deepcopy(dict(item))
            for item in missions
            if isinstance(item, Mapping)
        ]

    result["supplemental"] = copy.deepcopy(dict(supplemental))
    event = result.get("event")
    provenance = spec.get("provenance")
    if isinstance(event, dict) and isinstance(provenance, Mapping):
        source = event.get("source")
        if isinstance(source, dict):
            source["base_revision"] = str(
                provenance.get("base_revision")
                or provenance.get("source_revision")
                or ""
            )
            source["supplemental_digest"] = str(
                provenance.get("supplemental_digest") or ""
            )
            source["supplemental_provider"] = str(
                provenance.get("supplemental_provider") or ""
            )
    return result
