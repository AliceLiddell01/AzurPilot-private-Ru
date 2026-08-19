"""Детерминированная генерация campaign Python из проверенного MapSpec."""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from pathlib import Path

from deploy.atomic import file_remove, file_write, replace_tmp, to_tmp_file
from module.base.utils import node2location
from module.event_datamine.model import MapSpec
from module.event_datamine.runtime_policy import MapRuntimePolicy

_SAFE_CHAPTER_NAME = re.compile(r"[A-Za-z0-9_.-]+")
_SAFE_MODULE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_BOSS_CLEAR_LINES = {
    "campaign": "        return self.clear_boss()",
    "boss_fleet": "        return self.fleet_boss.clear_boss()",
    "fleet_1": "        return self.fleet_1.clear_boss()",
}


def map_module_name(chapter_name: str) -> str:
    """Преобразовать безопасное имя этапа в имя Python-модуля без путевой семантики."""

    raw = str(chapter_name or "")
    if not raw or not _SAFE_CHAPTER_NAME.fullmatch(raw):
        raise ValueError(f"Некорректный chapter_name для generated module: {raw!r}")
    name = raw.replace("-", "_").replace(".", "").lower()
    if not name:
        raise ValueError("chapter_name не может быть пустым")
    if name[0].isdigit():
        name = f"campaign_{name}"
    if not _SAFE_MODULE_NAME.fullmatch(name):
        raise ValueError(f"Некорректное имя generated map module: {name!r}")
    return name


def map_module_path(root: Path | str, module_name: str) -> Path:
    """Получить путь generated-модуля только внутри явно заданного каталога."""

    if not _SAFE_MODULE_NAME.fullmatch(str(module_name or "")):
        raise ValueError(
            f"Некорректное имя generated map module: {module_name!r}"
        )
    base = Path(root).resolve()
    target = (base / f"{module_name}.py").resolve()
    if target.parent != base:
        raise ValueError(
            f"Generated map module вышел за пределы каталога: {module_name!r}"
        )
    return target


def allocate_map_module_names(maps: Iterable[MapSpec]) -> tuple[str, ...]:
    """Детерминированно выделить уникальные имена модулей для набора карт."""

    used_names: set[str] = set()
    names: list[str] = []
    for spec in maps:
        base_name = map_module_name(spec.chapter_name)
        module_name = base_name
        if module_name in used_names:
            module_name = f"{base_name}_{spec.id}"
        if not _SAFE_MODULE_NAME.fullmatch(module_name):
            raise ValueError(
                f"Некорректное имя generated map module: {module_name!r}"
            )
        if module_name in used_names:
            raise ValueError(
                f"Неуникальное имя generated map module: {module_name}"
            )
        used_names.add(module_name)
        names.append(module_name)
    return tuple(names)


def _matrix(value: tuple[tuple[str, ...], ...]) -> list[str]:
    return ["    " + " ".join(row) for row in value]


def _has_spawn_kind(spec: MapSpec, kind: str) -> bool:
    """Определить сущность по структурным данным появления, а не по CV-шаблонам."""

    groups = (spec.spawn_data, spec.spawn_data_loop or ())
    return any(
        int(row.get(kind, 0) or 0) > 0
        for rows in groups
        for row in rows
    )


def _boss_clear_line(policy: MapRuntimePolicy) -> str:
    boss_clear = policy.boss_clear
    if boss_clear is None:
        raise ValueError(
            f"Карта {policy.map_id} не имеет проверенной runtime-policy очистки босса"
        )
    try:
        return _BOSS_CLEAR_LINES[boss_clear.strategy]
    except KeyError as exc:
        raise ValueError(
            f"Карта {policy.map_id} содержит неподдерживаемую boss strategy "
            f"{boss_clear.strategy!r}"
        ) from exc


def _validate_runtime_contract(spec: MapSpec, policy: MapRuntimePolicy) -> None:
    """Проверить полноту и геометрию runtime-контракта до генерации Python."""

    missing: list[str] = []
    if policy.boss_clear is None:
        missing.append("boss_clear")
    if policy.camera_calibration is None:
        missing.append("camera_calibration")
    if policy.detector_calibration is None:
        missing.append("detector_calibration")
    if policy.battle_plan is None:
        missing.append("battle_plan")
    if missing:
        raise ValueError(
            f"Карта {spec.id} не имеет полного runtime-контракта: {', '.join(missing)}"
        )

    camera = policy.camera_calibration
    assert camera is not None
    max_x, max_y = node2location(spec.shape)
    for node in (*camera.camera_data, *camera.spawn_points):
        x, y = node2location(node)
        if not (0 <= x <= max_x and 0 <= y <= max_y):
            raise ValueError(
                f"Runtime camera node {node!r} карты {spec.id} находится вне shape {spec.shape}"
            )

    battle_plan = policy.battle_plan
    assert battle_plan is not None
    seen_battles: set[int] = set()
    for step in battle_plan.siren_filter_steps:
        if step.battle in seen_battles:
            raise ValueError(
                f"Runtime battle_plan карты {spec.id} содержит повторный battle_{step.battle}"
            )
        seen_battles.add(step.battle)
        if step.battle == spec.boss_refresh:
            raise ValueError(
                f"Runtime battle_plan карты {spec.id} конфликтует с boss battle_{spec.boss_refresh}"
            )
        if step.battle > spec.boss_refresh:
            raise ValueError(
                f"Runtime battle_{step.battle} карты {spec.id} находится после появления босса"
            )


def _battle_plan_lines(policy: MapRuntimePolicy) -> list[str]:
    battle_plan = policy.battle_plan
    if battle_plan is None:
        raise ValueError(f"Карта {policy.map_id} не имеет проверенного battle_plan")
    lines = [f"    ENEMY_FILTER = {battle_plan.enemy_filter!r}"]
    for step in battle_plan.siren_filter_steps:
        lines.extend(
            [
                "",
                f"    def battle_{step.battle}(self):",
                "        if self.clear_siren():",
                "            return True",
                f"        if self.clear_filter_enemy(self.ENEMY_FILTER, preserve={step.preserve}):",
                "            return True",
                "",
                "        return self.battle_default()",
            ]
        )
    return lines


def generate_map_module(
    spec: MapSpec,
    *,
    runtime_policy: MapRuntimePolicy | None = None,
    base_import: str = "module.campaign.campaign_base",
) -> str:
    if spec.unknown_grid_types or spec.unknown_effects:
        raise ValueError(
            f"MapSpec карты {spec.id} не eligible: присутствуют неизвестные механики"
        )
    has_siren = _has_spawn_kind(spec, "siren")
    if has_siren and (
        runtime_policy is None
        or runtime_policy.siren_recognition is None
    ):
        raise ValueError(
            f"Карта {spec.id} содержит siren, но не имеет проверенной "
            "runtime-policy распознавания"
        )
    if runtime_policy is None:
        raise ValueError(f"Карта {spec.id} не имеет проверенной runtime-policy")
    if runtime_policy.map_id != spec.id:
        raise ValueError(
            f"Runtime-policy карты {runtime_policy.map_id} "
            f"не соответствует MapSpec {spec.id}"
        )
    if runtime_policy.chapter_name.casefold() != spec.chapter_name.casefold():
        raise ValueError(
            f"Runtime-policy карты {spec.id} относится к другому chapter_name"
        )
    _validate_runtime_contract(spec, runtime_policy)
    boss_clear_line = _boss_clear_line(runtime_policy)
    camera = runtime_policy.camera_calibration
    assert camera is not None

    lines = [
        f"from {base_import} import CampaignBase",
        "from module.map.map_base import CampaignMap",
        "",
        f"MAP = CampaignMap({spec.chapter_name!r})",
        f"MAP.shape = {spec.shape!r}",
        f"MAP.camera_data = {list(camera.camera_data)!r}",
        f"MAP.camera_data_spawn_point = {list(camera.spawn_points)!r}",
    ]
    if spec.portals:
        lines.append(
            f"MAP.portal_data = "
            f"{[(item.source, item.target) for item in spec.portals]!r}"
        )
    lines.extend(['MAP.map_data = """', *_matrix(spec.map_data), '"""'])
    if spec.map_data_loop:
        lines.extend(
            ['MAP.map_data_loop = """', *_matrix(spec.map_data_loop), '"""']
        )
    lines.extend(
        [
            'MAP.weight_data = """',
            *[
                "    " + " ".join("50" for _ in row)
                for row in spec.map_data
            ],
            '"""',
        ]
    )
    if spec.land_based:
        lines.append(f"MAP.land_based_data = {list(spec.land_based)!r}")
    lines.append(f"MAP.spawn_data = {list(spec.spawn_data)!r}")
    if spec.spawn_data_loop:
        lines.append(
            f"MAP.spawn_data_loop = {list(spec.spawn_data_loop)!r}"
        )
    lines.extend(
        [
            "",
            "class Config:",
            "    # Только структурные факты карты из ShareCfg.",
        ]
    )
    factual = {
        "MAP_HAS_MAP_STORY": spec.has_story,
        "MAP_HAS_FLEET_STEP": spec.has_fleet_step,
        "MAP_HAS_AMBUSH": spec.has_ambush,
        "MAP_HAS_MYSTERY": spec.has_mystery,
        "MAP_HAS_PORTAL": bool(spec.portals),
        "MAP_HAS_LAND_BASED": bool(spec.land_based),
        "MAP_HAS_SIREN": has_siren,
        "MAP_HAS_MOVABLE_ENEMY": bool(spec.movable_enemy_turns),
        "STAR_REQUIRE_1": spec.star_requirements[0],
        "STAR_REQUIRE_2": spec.star_requirements[1],
        "STAR_REQUIRE_3": spec.star_requirements[2],
    }
    for key, value in factual.items():
        lines.append(f"    {key} = {value!r}")
    if spec.movable_enemy_turns:
        lines.append(
            f"    MOVABLE_ENEMY_TURN = {tuple(spec.movable_enemy_turns)!r}"
        )
    lines.append(
        "    # Проверенные runtime-факты из ограниченной policy generated package."
    )
    for key, value in runtime_policy.config_items():
        lines.append(f"    {key} = {value!r}")
    lines.extend(
        [
            "",
            "class Campaign(CampaignBase):",
            "    MAP = MAP",
        ]
    )
    lines.extend(_battle_plan_lines(runtime_policy))
    lines.extend(
        [
            "",
            f"    def battle_{spec.boss_refresh}(self):",
            boss_clear_line,
            "",
        ]
    )
    result = "\n".join(lines) + "\n"
    ast.parse(result)
    return result


def write_map_module(
    path: Path | str,
    content: str,
    *,
    overwrite: bool = False,
) -> Path:
    target = Path(path)
    ast.parse(content)
    if target.exists() and not overwrite:
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = to_tmp_file(str(target))
    try:
        file_write(temp, content)
        replace_tmp(temp, str(target))
    except BaseException:
        try:
            file_remove(temp)
        except OSError:
            pass
        raise
    return target
