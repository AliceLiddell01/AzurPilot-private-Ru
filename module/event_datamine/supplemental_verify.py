"""Cross-source проверки и точечные supplemental-исправления EventSpec."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from module.event_datamine.supplemental import (
    EventSupplementalError,
    require_list,
    require_mapping,
)

_DISPLAY_EXTENSIONS = (".png", ".svg", ".webp")


def validate_base_contract(
    spec: Mapping[str, Any], supplemental: Mapping[str, Any]
) -> None:
    base = require_mapping(supplemental["base_contract"], "base_contract")
    provenance = (
        spec.get("provenance") if isinstance(spec.get("provenance"), Mapping) else {}
    )
    actual = {
        "activity_id": int(provenance.get("activity_id", 0) or 0),
        "event_name": str(spec.get("name") or ""),
        "map_count": len(spec.get("maps", [])),
        "milestone_count": len(spec.get("milestones", [])),
        "server": str(spec.get("server") or "").upper(),
        "shop_count": len(spec.get("shop_items", [])),
        "source_revision": str(provenance.get("revision") or ""),
    }
    expected = {
        "activity_id": int(base.get("activity_id", 0) or 0),
        "event_name": str(base.get("event_name") or ""),
        "map_count": int(base.get("map_count", 0) or 0),
        "milestone_count": int(base.get("milestone_count", 0) or 0),
        "server": str(base.get("server") or "").upper(),
        "shop_count": int(base.get("shop_count", 0) or 0),
        "source_revision": str(base.get("source_revision") or ""),
    }
    if str(spec.get("id") or "") != str(supplemental.get("event_id") or ""):
        raise EventSupplementalError("Supplemental относится к другой Event identity")
    for field, expected_value in expected.items():
        if actual[field] != expected_value:
            raise EventSupplementalError(
                f"base_contract не совпадает с EventSpec: {field}: "
                f"expected={expected_value!r}, actual={actual[field]!r}"
            )


def validate_external_tables(
    spec: Mapping[str, Any], supplemental: Mapping[str, Any]
) -> None:
    verification = require_mapping(supplemental["verification"], "verification")
    shop = require_mapping(verification["shop"], "verification.shop")
    shop_items = [
        item for item in spec.get("shop_items", []) if isinstance(item, Mapping)
    ]
    row_count = len(shop_items)
    full_buyout = sum(
        int(item.get("price", 0) or 0) * int(item.get("stock", 0) or 0)
        for item in shop_items
    )
    featured_row_id = int(shop.get("featured_item_row_id", 0) or 0)
    featured = next(
        (
            item
            for item in shop_items
            if int(item.get("row_id", 0) or 0) == featured_row_id
        ),
        None,
    )
    if featured is None:
        raise EventSupplementalError(
            f"Wiki shop verification не нашёл featured row {featured_row_id}"
        )
    expected_featured_stock = int(shop.get("featured_item_full_stock", 0) or 0)
    actual_featured_stock = int(featured.get("stock", 0) or 0)
    if actual_featured_stock != expected_featured_stock:
        raise EventSupplementalError(
            "Featured shop stock не совпадает: "
            f"expected={expected_featured_stock}, actual={actual_featured_stock}"
        )
    one_copy_stock = int(shop.get("featured_item_one_copy_stock", 0) or 0)
    if one_copy_stock < 0 or one_copy_stock > actual_featured_stock:
        raise EventSupplementalError("Некорректный featured_item_one_copy_stock")
    one_featured_copy_buyout = full_buyout - (
        actual_featured_stock - one_copy_stock
    ) * int(featured.get("price", 0) or 0)
    expected_shop = {
        "row_count": int(shop.get("row_count", 0) or 0),
        "full_buyout_cost": int(shop.get("full_buyout_cost", 0) or 0),
        "one_featured_copy_buyout_cost": int(
            shop.get("one_featured_copy_buyout_cost", 0) or 0
        ),
    }
    actual_shop = {
        "row_count": row_count,
        "full_buyout_cost": full_buyout,
        "one_featured_copy_buyout_cost": one_featured_copy_buyout,
    }
    if actual_shop != expected_shop:
        raise EventSupplementalError(
            "Wiki shop verification не совпадает с ShareCfg: "
            f"expected={expected_shop}, actual={actual_shop}"
        )

    milestones = require_mapping(
        verification["milestones"], "verification.milestones"
    )
    actual_thresholds = [
        int(item.get("threshold", 0) or 0)
        for item in spec.get("milestones", [])
        if isinstance(item, Mapping)
    ]
    expected_thresholds = [
        int(value)
        for value in require_list(milestones.get("thresholds"), "thresholds")
    ]
    if (
        len(actual_thresholds) != int(milestones.get("count", 0) or 0)
        or actual_thresholds != expected_thresholds
    ):
        raise EventSupplementalError(
            "Wiki milestone verification не совпадает с ShareCfg"
        )


def _display_asset_path(asset_root: Path, kind: str, game_id: int) -> str:
    display_root = (asset_root / "webui" / "event_shop").resolve()
    for extension in _DISPLAY_EXTENSIONS:
        candidate = (display_root / f"{kind}-{game_id}{extension}").resolve()
        if display_root in candidate.parents and candidate.is_file():
            return candidate.relative_to(asset_root.resolve()).as_posix()
    return ""


def apply_resource_display_assets(
    spec: dict[str, Any],
    supplemental: Mapping[str, Any],
    *,
    asset_root: Path,
) -> set[int]:
    declared = {
        int(item["resource_id"]): str(item.get("name") or "")
        for item in supplemental.get("resource_display_assets", [])
        if isinstance(item, Mapping)
    }
    resolved: dict[int, str] = {}
    for resource_id in declared:
        relative = _display_asset_path(asset_root, "resource", resource_id)
        if not relative:
            raise EventSupplementalError(
                f"Не найден локальный display asset resource-{resource_id}"
            )
        resolved[resource_id] = relative

    def mark(asset: Any) -> None:
        if not isinstance(asset, dict):
            return
        if str(asset.get("kind") or "") != "resource":
            return
        try:
            resource_id = int(asset.get("game_id", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            return
        if resource_id in resolved:
            asset["display_resolved"] = True
            asset["display_path"] = resolved[resource_id]
            name = declared.get(resource_id)
            if name:
                asset["display_name"] = name

    for item in spec.get("shop_items", []):
        if isinstance(item, dict):
            mark(item.get("asset"))
    for milestone in spec.get("milestones", []):
        if not isinstance(milestone, dict):
            continue
        for reward in milestone.get("rewards", []):
            if not isinstance(reward, dict):
                continue
            mark(reward.get("asset"))
            if (
                int(reward.get("reward_type", 0) or 0) == 1
                and int(reward.get("reward_id", 0) or 0) in declared
                and not str(reward.get("name") or "").strip()
            ):
                reward["name"] = declared[int(reward["reward_id"])]
    return set(resolved)


def finding_resource_is_resolved(
    finding: Mapping[str, Any],
    spec: Mapping[str, Any],
    resource_ids: set[int],
) -> bool:
    if str(finding.get("code") or "") != "asset_unresolved":
        return False
    path = str(finding.get("path") or "")
    shop_match = re.fullmatch(r"shop\.([0-9]+)\.asset", path)
    if shop_match:
        row_id = int(shop_match.group(1))
        item = next(
            (
                row
                for row in spec.get("shop_items", [])
                if isinstance(row, Mapping)
                and int(row.get("row_id", 0) or 0) == row_id
            ),
            None,
        )
        if not isinstance(item, Mapping):
            return False
        asset = item.get("asset")
        return (
            isinstance(asset, Mapping)
            and str(asset.get("kind") or "") == "resource"
            and int(asset.get("game_id", 0) or 0) in resource_ids
        )

    milestone_match = re.fullmatch(r"milestones\.([0-9]+)", path)
    if milestone_match:
        index = int(milestone_match.group(1))
        milestones = spec.get("milestones", [])
        if not isinstance(milestones, list) or not 0 <= index < len(milestones):
            return False
        milestone = milestones[index]
        if not isinstance(milestone, Mapping):
            return False
        rewards = [
            reward
            for reward in milestone.get("rewards", [])
            if isinstance(reward, Mapping)
        ]
        return bool(rewards) and all(
            int(reward.get("reward_type", 0) or 0) == 1
            and int(reward.get("reward_id", 0) or 0) in resource_ids
            for reward in rewards
        )
    return False


def apply_shop_overrides(
    spec: dict[str, Any], supplemental: Mapping[str, Any]
) -> set[str]:
    fixed_paths: set[str] = set()
    rows = {
        int(item.get("row_id", 0) or 0): item
        for item in spec.get("shop_items", [])
        if isinstance(item, dict)
    }
    for override in supplemental.get("shop_overrides", []):
        if not isinstance(override, Mapping):
            continue
        row_id = int(override.get("row_id", 0) or 0)
        row = rows.get(row_id)
        if row is None:
            raise EventSupplementalError(f"Shop row {row_id} отсутствует в EventSpec")
        expected = {
            "item_type": int(override.get("expected_item_type", 0) or 0),
            "item_id": int(override.get("expected_item_id", 0) or 0),
            "price": int(override.get("expected_price", 0) or 0),
            "stock": int(override.get("expected_stock", 0) or 0),
        }
        actual = {
            "item_type": int(row.get("item_type", 0) or 0),
            "item_id": int(row.get("item_id", 0) or 0),
            "price": int(row.get("price", 0) or 0),
            "stock": int(row.get("stock", 0) or 0),
        }
        if actual != expected:
            raise EventSupplementalError(
                f"Shop row {row_id} не совпадает с supplemental identity: "
                f"expected={expected}, actual={actual}"
            )
        name = str(override.get("name") or "").strip()
        if not name:
            raise EventSupplementalError(
                f"Shop row {row_id} не содержит supplemental name"
            )
        row["name"] = name
        row["name_source"] = "supplemental"
        fixed_paths.add(f"shop.{row_id}.name")
    return fixed_paths


def apply_task_classification(
    spec: dict[str, Any], supplemental: Mapping[str, Any]
) -> set[str]:
    sources = {
        str(item.get("id") or ""): item
        for item in spec.get("pt_sources", [])
        if isinstance(item, dict)
    }
    fixed_paths: set[str] = set()
    for classification in supplemental.get("task_classification", []):
        if not isinstance(classification, Mapping):
            continue
        task_id = int(classification.get("task_id", 0) or 0)
        identity = f"task:{task_id}"
        source = sources.get(identity)
        if source is None:
            raise EventSupplementalError(f"PT source {identity} отсутствует")
        expected_name = str(classification.get("expected_name") or "")
        expected_points = int(classification.get("expected_points", 0) or 0)
        if (
            str(source.get("name") or "") != expected_name
            or int(source.get("points", 0) or 0) != expected_points
        ):
            raise EventSupplementalError(
                f"PT source {identity} не совпадает с supplemental contract"
            )
        kind = str(classification.get("kind") or "")
        source["kind"] = kind
        source["recurring"] = kind in {"daily", "weekly", "daily_first_clear"}
        source["classification_source"] = "supplemental"
        scope = str(classification.get("scope") or "")
        if scope:
            source["scope"] = scope
        fixed_paths.add(f"pt_sources.{identity}")
    return fixed_paths
