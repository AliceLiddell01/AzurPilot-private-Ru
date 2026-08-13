"""Компилятор полного EventSpec из закреплённого ShareCfg snapshot."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any

from module.event_datamine.map_compiler import MapCompiler, _at, _values
from module.event_datamine.model import (
    AssetReference,
    CurrencySpec,
    EventSpec,
    MilestoneSpec,
    Provenance,
    PtSourceSpec,
    RewardSpec,
    ShopItemSpec,
    ValidationFinding,
)
from module.event_datamine.patches import patches_for
from module.event_datamine.source import ShareCfgError, ShareCfgLoader

RESOURCE_NAMES = {1: "Coins", 2: "Oil", 4: "Gems", 14: "Medals"}
ITEM_CATEGORIES = {1: "resource", 2: "item", 3: "equipment", 4: "ship"}
RUNTIME_FILTER_BY_GAME_ID = {
    (1, 1): "Coin",
    (1, 2): "Oil",
    (2, 15008): "Chip",
    (2, 15012): "Array",
    (2, 15014): "AugmentCoreT3",
    (2, 15016): "AugmentEnhanceT2",
    (2, 15020): "AugmentChangeT1",
    (2, 15021): "AugmentChangeT2",
    (2, 17003): "PlateGeneralT3",
    (2, 17013): "PlateGunT3",
    (2, 17023): "PlateTorpedoT3",
    (2, 17033): "PlateAntiairT3",
    (2, 17043): "PlatePlaneT3",
    (2, 20011): "CatT1",
    (2, 20012): "CatT2",
    (2, 20013): "CatT3",
    (2, 21048): "Meta",
    (2, 30014): "BoxT4",
    (2, 30024): "BoxT4",
    (2, 30034): "BoxT4",
    (2, 30044): "BoxT4",
    (2, 30368): "SkinBox",
    (2, 42060): "PRS7",
    (2, 42066): "DRS7",
    (2, 50001): "FoodT1",
    (3, 24400): "EquipUR",
    (4, 9707071): "ShipSSR",
}
_CJK_TEXT = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_BLUEPRINT_SERIES = re.compile(
    r"^(Special )?General Blueprint - Series (\d+)$", re.IGNORECASE
)


def _date_part(value: Any) -> str:
    date = _at(value, 0, {})
    clock = _at(value, 1, {})
    try:
        return datetime(  # noqa: DTZ001 - ShareCfg хранит server-local wall time без offset.
            int(_at(date, 0)),
            int(_at(date, 1)),
            int(_at(date, 2)),
            int(_at(clock, 0, 0)),
            int(_at(clock, 1, 0)),
            int(_at(clock, 2, 0)),
        ).isoformat(sep=" ")
    except TypeError, ValueError:
        return ""


def _activity_times(row: Mapping[str, Any]) -> tuple[str, str]:
    value = row.get("time")
    return _date_part(_at(value, 1, {})), _date_part(_at(value, 2, {}))


def classify_pt_task_config(
    configs: Any, *, task_ids: set[int], map_ids: set[int]
) -> tuple[dict[int, str], set[int]]:
    """Classify only by the numeric source relation encoded in taskConfig."""
    group_kinds = {
        1: "first_clear",
        2: "daily_first_clear",
        3: "daily",
        4: "challenge",
    }
    tasks: dict[int, str] = {}
    daily_first_clear_maps: set[int] = set()
    for config in _values(configs):
        kind = group_kinds.get(int(_at(config, 0, 0) or 0), "unknown")
        for value in _values(_at(config, 2, {})):
            if not isinstance(value, int):
                continue
            if value in task_ids:
                tasks[value] = kind
            elif kind == "daily_first_clear" and value in map_ids:
                daily_first_clear_maps.add(value)
    return tasks, daily_first_clear_maps


class EventCompiler:
    SCHEMA_VERSION = 2

    def __init__(self, source: ShareCfgLoader) -> None:
        self.source = source
        self.findings: list[ValidationFinding] = []

    def _table(self, name: str, *, required: bool = True) -> dict[int, Any]:
        try:
            return self.source.load_table(name)
        except ShareCfgError as exc:
            severity = "error" if required else "warning"
            self.findings.append(
                ValidationFinding(exc.code, severity, str(exc), f"source.{name}")
            )
            return {}

    @staticmethod
    def _linked_name(
        activity_id: int, memories: Mapping[int, Any], medals: Mapping[int, Any]
    ) -> str:
        for row in memories.values():
            if (
                isinstance(row, Mapping)
                and int(row.get("link_event", 0) or 0) == activity_id
            ):
                title = str(row.get("title") or "").strip()
                if title:
                    return title
        for row in medals.values():
            if not isinstance(row, Mapping):
                continue
            for link in _values(row.get("activity_link")):
                if int(_at(link, 1, 0) or 0) == activity_id:
                    title = str(row.get("group_name") or "").strip()
                    if title:
                        return title
        return ""

    @staticmethod
    def _item_record(
        item_id: int, normal: Mapping[int, Any], virtual: Mapping[int, Any]
    ) -> Mapping[str, Any]:
        row = normal.get(item_id)
        if isinstance(row, Mapping):
            return row
        row = virtual.get(item_id)
        return row if isinstance(row, Mapping) else {}

    @classmethod
    def _game_record(
        cls,
        item_type: int,
        item_id: int,
        normal: Mapping[int, Any],
        virtual: Mapping[int, Any],
        equipment: Mapping[int, Any],
        ships: Mapping[int, Any],
    ) -> Mapping[str, Any]:
        if item_type == 2:
            return cls._item_record(item_id, normal, virtual)
        if item_type == 3:
            row = equipment.get(item_id)
            return row if isinstance(row, Mapping) else {}
        if item_type == 4:
            row = ships.get(item_id)
            return row if isinstance(row, Mapping) else {}
        return {}

    def _asset(
        self,
        *,
        kind: str,
        game_id: int,
        row: Mapping[str, Any] | None = None,
        path: str = "",
    ) -> AssetReference:
        source_path = str((row or {}).get("icon") or path)
        if not source_path and kind == "ship" and (row or {}).get("skin_id"):
            source_path = f"ship_skin/{int((row or {})['skin_id'])}"
        elif source_path and kind == "equipment":
            source_path = f"Equips/{source_path}"
        return AssetReference(
            kind=kind,
            game_id=str(game_id),
            source_path=source_path,
            resolved=bool(source_path),
        )

    def _name(self, value: Any, fallback: str, path: str) -> str:
        name = str(value or "").strip()
        if name and not (
            self.source.snapshot.server == "EN" and _CJK_TEXT.search(name)
        ):
            return name
        if name:
            self.findings.append(
                ValidationFinding(
                    "source_name_unlocalized",
                    "warning",
                    "EN ShareCfg содержит нелокализованное имя; используется техническая identity",
                    path,
                )
            )
        return fallback

    @staticmethod
    def _runtime_filter(
        *,
        item_type: int,
        item_id: int,
        name: str,
        rarity: int | None,
        source_path: str,
    ) -> str:
        known = RUNTIME_FILTER_BY_GAME_ID.get((item_type, item_id), "")
        if known:
            return known
        if item_type == 4 and rarity == 5:
            return "ShipSSR"
        if item_type == 3 and rarity == 5:
            return "EquipSSR"
        if "appearancebox" in source_path.lower() or "gear skin box" in name.lower():
            return "SkinBox"
        blueprint = _BLUEPRINT_SERIES.fullmatch(name)
        if blueprint:
            return f"{'DR' if blueprint.group(1) else 'PR'}S{blueprint.group(2)}"
        return ""

    def _reward(
        self,
        value: Any,
        normal_items: Mapping[int, Any],
        virtual_items: Mapping[int, Any],
        equipment: Mapping[int, Any],
        ships: Mapping[int, Any],
        path: str,
    ) -> RewardSpec:
        reward_type = int(_at(value, 0, 0) or 0)
        reward_id = int(_at(value, 1, 0) or 0)
        amount = int(_at(value, 2, 1) or 0)
        row = self._game_record(
            reward_type, reward_id, normal_items, virtual_items, equipment, ships
        )
        name = self._name(
            row.get("name") or RESOURCE_NAMES.get(reward_id),
            f"Game reward {reward_type}:{reward_id}",
            f"{path}.name",
        )
        asset = self._asset(
            kind=ITEM_CATEGORIES.get(reward_type, "unknown"), game_id=reward_id, row=row
        )
        if not asset.resolved:
            self.findings.append(
                ValidationFinding(
                    "asset_unresolved",
                    "warning",
                    f"Не разрешён asset награды {reward_type}:{reward_id}",
                    path,
                )
            )
        return RewardSpec(
            reward_type,
            reward_id,
            amount,
            name,
            int(row.get("rarity")) if row.get("rarity") is not None else None,
            asset,
        )

    def _shop(
        self,
        activity: Mapping[str, Any] | None,
        rows: Mapping[int, Any],
        normal_items: Mapping[int, Any],
        virtual_items: Mapping[int, Any],
        equipment: Mapping[int, Any],
        ships: Mapping[int, Any],
    ) -> tuple[tuple[ShopItemSpec, ...], set[int]]:
        if not isinstance(activity, Mapping):
            self.findings.append(
                ValidationFinding(
                    "shop_activity_missing",
                    "warning",
                    "Связанная activity магазина не найдена",
                    "shop",
                )
            )
            return (), set()
        result: list[ShopItemSpec] = []
        currencies: set[int] = set()
        seen_rows: set[int] = set()
        for row_id in _values(activity.get("config_data")):
            if int(row_id) in seen_rows:
                self.findings.append(
                    ValidationFinding(
                        "duplicate_shop_row",
                        "error",
                        f"Строка магазина {row_id} указана повторно",
                        "shop",
                    )
                )
                continue
            seen_rows.add(int(row_id))
            row = rows.get(int(row_id))
            if not isinstance(row, Mapping):
                self.findings.append(
                    ValidationFinding(
                        "shop_row_missing",
                        "error",
                        f"Строка магазина {row_id} отсутствует",
                        f"shop.{row_id}",
                    )
                )
                continue
            item_type = int(row.get("commodity_type", 0) or 0)
            item_id = int(row.get("commodity_id", 0) or 0)
            currency_id = int(row.get("resource_type", 0) or 0)
            price = int(row.get("resource_num", 0) or 0)
            stock = int(row.get("num_limit", 0) or 0)
            if not currency_id or price <= 0 or stock <= 0:
                self.findings.append(
                    ValidationFinding(
                        "invalid_shop_row",
                        "error",
                        f"Строка магазина {row_id} имеет недопустимую валюту, цену или stock",
                        f"shop.{row_id}",
                    )
                )
            currencies.add(currency_id)
            item_row = self._game_record(
                item_type, item_id, normal_items, virtual_items, equipment, ships
            )
            if item_type == 1:
                name = RESOURCE_NAMES.get(item_id, f"Game resource {item_id}")
            else:
                name = self._name(
                    item_row.get("name"),
                    f"Game item {item_type}:{item_id}",
                    f"shop.{row_id}.name",
                )
            asset = self._asset(
                kind=ITEM_CATEGORIES.get(item_type, "unknown"),
                game_id=item_id,
                row=item_row,
            )
            if not asset.resolved:
                self.findings.append(
                    ValidationFinding(
                        "asset_unresolved",
                        "warning",
                        f"Не разрешён asset товара {item_type}:{item_id}",
                        f"shop.{row_id}.asset",
                    )
                )
            result.append(
                ShopItemSpec(
                    row_id=int(row_id),
                    item_type=item_type,
                    item_id=item_id,
                    amount=int(row.get("num", 1) or 1),
                    price=price,
                    currency_id=currency_id,
                    stock=stock,
                    name=name,
                    category=ITEM_CATEGORIES.get(item_type, "unknown"),
                    rarity=int(item_row.get("rarity"))
                    if item_row.get("rarity") is not None
                    else None,
                    event_shop_filter=self._runtime_filter(
                        item_type=item_type,
                        item_id=item_id,
                        name=name,
                        rarity=(
                            int(item_row.get("rarity"))
                            if item_row.get("rarity") is not None
                            else None
                        ),
                        source_path=asset.source_path,
                    ),
                    asset=asset,
                    limit_args=row.get("limit_args", ""),
                )
            )
        return tuple(sorted(result, key=lambda item: item.row_id)), currencies

    def compile(self, activity_id: int) -> EventSpec:
        self.findings = []
        activities = self._table("activity_template")
        root = activities.get(activity_id)
        if not isinstance(root, Mapping):
            raise ShareCfgError(
                "activity_missing",
                f"Activity {activity_id} отсутствует",
                table="activity_template",
            )
        mark = int(root.get("mark", 0) or 0)
        related = {
            int(row_id): row
            for row_id, row in activities.items()
            if isinstance(row, Mapping) and int(row.get("mark", 0) or 0) == mark
        }

        memories = self._table("memory_group", required=False)
        medals = self._table("activity_medal_group", required=False)
        name = self._linked_name(activity_id, memories, medals)
        if not name:
            name = f"Activity {activity_id}"
            self.findings.append(
                ValidationFinding(
                    "event_name_missing",
                    "warning",
                    "Читаемое имя события не найдено; используется техническое",
                    "event.name",
                )
            )
        farm_start, farm_end = _activity_times(root)
        if not farm_start or not farm_end:
            self.findings.append(
                ValidationFinding(
                    "event_time_missing",
                    "error",
                    "Не удалось декодировать время активности",
                    "event.time",
                )
            )

        normal_items = self._table("item_data_statistics", required=False)
        virtual_items = self._table("item_virtual_data_statistics", required=False)
        resources = self._table("player_resource", required=False)
        equipment = self._table("equip_data_statistics", required=False)
        ships = self._table("ship_data_statistics", required=False)
        pt_rows = self._table("activity_event_pt", required=False)
        milestone_activity = next(
            (row for row in related.values() if int(row.get("type", 0) or 0) == 74),
            None,
        )
        milestone_row = None
        if isinstance(milestone_activity, Mapping):
            milestone_row = pt_rows.get(
                int(
                    milestone_activity.get("config_id", milestone_activity.get("id", 0))
                    or 0
                )
            )
        milestones: list[MilestoneSpec] = []
        currency_ids: set[int] = set()
        if isinstance(milestone_row, Mapping):
            currency_ids.add(int(milestone_row.get("pt", 0) or 0))
            targets = _values(milestone_row.get("target"))
            rewards = _values(milestone_row.get("drop_client"))
            if len(targets) != len(rewards):
                self.findings.append(
                    ValidationFinding(
                        "milestone_length_mismatch",
                        "error",
                        "Количество milestone thresholds и rewards различается",
                        "milestones",
                    )
                )
            for index, (target, reward) in enumerate(zip(targets, rewards)):
                milestones.append(
                    MilestoneSpec(
                        int(target),
                        (
                            self._reward(
                                reward,
                                normal_items,
                                virtual_items,
                                equipment,
                                ships,
                                f"milestones.{index}",
                            ),
                        ),
                    )
                )
            thresholds = [item.threshold for item in milestones]
            if thresholds != sorted(set(thresholds)):
                self.findings.append(
                    ValidationFinding(
                        "milestone_order_invalid",
                        "error",
                        "Milestone thresholds должны быть строго возрастающими и уникальными",
                        "milestones",
                    )
                )
        else:
            self.findings.append(
                ValidationFinding(
                    "milestone_missing",
                    "warning",
                    "Milestone activity не обнаружена",
                    "milestones",
                )
            )

        shop_id = 0
        if isinstance(milestone_activity, Mapping):
            client = milestone_activity.get("config_client")
            if isinstance(client, Mapping):
                shop_id = int(client.get("shopLinkActID", 0) or 0)
        shop_activity = (
            activities.get(shop_id)
            if shop_id
            else next(
                (row for row in related.values() if int(row.get("type", 0) or 0) == 14),
                None,
            )
        )
        shop_items, shop_currencies = self._shop(
            shop_activity,
            self._table("activity_shop_template", required=False),
            normal_items,
            virtual_items,
            equipment,
            ships,
        )
        currency_ids.update(shop_currencies)
        _, shop_end = (
            _activity_times(shop_activity)
            if isinstance(shop_activity, Mapping)
            else ("", "")
        )

        chapters = self._table("chapter_template")
        loops = self._table("chapter_template_loop", required=False)
        map_ids: set[int] = set()
        for row in related.values():
            if int(row.get("type", 0) or 0) == 12:
                map_ids.update(int(value) for value in _values(row.get("config_data")))
        map_compiler = MapCompiler(
            chapters,
            loops,
            self._table("map_event_list", required=False),
            self._table("map_event_template", required=False),
            self._table("expedition_data_template", required=False),
        )
        maps = []
        for map_id in sorted(map_ids):
            spec, findings = map_compiler.compile(map_id)
            patches = patches_for(
                f"{self.source.snapshot.server.lower()}:{activity_id}", map_id
            )
            if patches:
                findings = [
                    item
                    for item in findings
                    if not (
                        item.code == "unknown_land_rotation"
                        and "rotation 10" in item.message
                        and any("code 10" in patch.expected_effect for patch in patches)
                    )
                ]
                if spec is not None:
                    spec = replace(
                        spec, compatibility_patch_ids=tuple(item.id for item in patches)
                    )
            self.findings.extend(findings)
            if spec is not None:
                maps.append(spec)
        if not maps:
            self.findings.append(
                ValidationFinding(
                    "event_maps_missing", "error", "Карты события не обнаружены", "maps"
                )
            )

        tasks = self._table("task_data_template", required=False)
        pt_sources: list[PtSourceSpec] = []
        task_ids: set[int] = set()
        task_kinds: dict[int, str] = {}
        daily_first_clear_map_ids: set[int] = set()
        runtime_currency_tokens: dict[int, str] = {}
        for row in related.values():
            if int(row.get("type", 0) or 0) == 13:
                task_ids.update(int(value) for value in _values(row.get("config_data")))
            client = row.get("config_client")
            if isinstance(client, Mapping):
                for field, token in (("ptId", "pt"), ("uPtId", "URpt")):
                    currency_id = int(client.get(field, 0) or 0)
                    if currency_id:
                        runtime_currency_tokens[currency_id] = token
                classified_tasks, classified_maps = classify_pt_task_config(
                    client.get("taskConfig"),
                    task_ids=set(tasks),
                    map_ids=map_ids,
                )
                task_ids.update(classified_tasks)
                task_kinds.update(classified_tasks)
                daily_first_clear_map_ids.update(classified_maps)
        for task_id in sorted(task_ids):
            task = tasks.get(task_id)
            if not isinstance(task, Mapping):
                continue
            for reward in _values(task.get("award_display")):
                if (
                    int(_at(reward, 0, 0) or 0) == 1
                    and int(_at(reward, 1, 0) or 0) in currency_ids
                ):
                    kind = task_kinds.get(task_id, "unknown")
                    if kind == "unknown":
                        self.findings.append(
                            ValidationFinding(
                                "pt_source_kind_unknown",
                                "warning",
                                f"Не классифицирована структурная связь PT task {task_id}",
                                f"pt_sources.task:{task_id}",
                            )
                        )
                    pt_sources.append(
                        PtSourceSpec(
                            f"task:{task_id}",
                            kind,
                            str(task.get("desc") or task.get("name") or task_id),
                            int(_at(reward, 2, 0) or 0),
                            kind in {"daily", "weekly", "daily_first_clear"},
                            (task_id,),
                        )
                    )
        for map_id in sorted(daily_first_clear_map_ids):
            chapter = chapters.get(map_id, {})
            pt_sources.append(
                PtSourceSpec(
                    f"map-daily-first-clear:{map_id}",
                    "daily_first_clear",
                    str(chapter.get("chapter_name") or map_id),
                    None,
                    True,
                    (map_id,),
                )
            )
        for map_id in sorted(map_ids):
            chapter = chapters.get(map_id, {})
            pt_sources.append(
                PtSourceSpec(
                    f"map:{map_id}",
                    "repeatable_map_clear",
                    str(chapter.get("chapter_name") or map_id),
                    None,
                    False,
                    (map_id,),
                )
            )
        self.findings.append(
            ValidationFinding(
                "map_pt_amount_unavailable",
                "warning",
                "ShareCfg snapshot не содержит достоверное количество PT за повторное прохождение карты",
                "pt_sources.maps",
            )
        )

        if len(currency_ids) == 1 and not runtime_currency_tokens:
            runtime_currency_tokens[next(iter(currency_ids))] = "pt"
        currencies = []
        for currency_id in sorted(value for value in currency_ids if value):
            resource = resources.get(currency_id, {})
            resource = resource if isinstance(resource, Mapping) else {}
            item_id = int(resource.get("itemid", 0) or 0)
            item = self._item_record(item_id, normal_items, virtual_items)
            currencies.append(
                CurrencySpec(
                    currency_id,
                    str(
                        item.get("name")
                        or RESOURCE_NAMES.get(currency_id)
                        or f"Event currency {currency_id}"
                    ),
                    self._asset(
                        kind="activity_currency",
                        game_id=currency_id,
                        row=item,
                        path=f"activity_currency/{currency_id}",
                    ),
                    runtime_currency_tokens.get(currency_id, ""),
                )
            )
        errors = any(item.severity == "error" for item in self.findings)
        partial_codes = {
            "map_pt_amount_unavailable",
            "asset_unresolved",
            "milestone_missing",
            "shop_activity_missing",
            "source_name_unlocalized",
        }
        is_partial = any(item.code in partial_codes for item in self.findings)
        status = "unsupported" if errors else "partial" if is_partial else "verified"
        provenance = Provenance(
            provider=self.source.snapshot.provider,
            repository=self.source.snapshot.repository,
            revision=self.source.snapshot.revision,
            server=self.source.snapshot.server,
            activity_id=activity_id,
            schema_version=self.SCHEMA_VERSION,
        )
        return EventSpec(
            id=f"{self.source.snapshot.server.lower()}:{activity_id}",
            name=name,
            server=self.source.snapshot.server,
            farm_start=farm_start,
            farm_end=farm_end,
            shop_end=shop_end,
            source_status=status,
            provenance=provenance,
            related_activity_ids=tuple(sorted(related)),
            currencies=tuple(currencies),
            maps=tuple(maps),
            shop_items=shop_items,
            milestones=tuple(milestones),
            pt_sources=tuple(pt_sources),
            findings=tuple(self.findings),
        )
