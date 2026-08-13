"""Детерминированная генерация campaign Python из уже проверенного MapSpec."""

from __future__ import annotations

import ast
from pathlib import Path

from deploy.atomic import file_remove, file_write, replace_tmp, to_tmp_file
from module.event_datamine.model import MapSpec
from module.event_datamine.patches import CompatibilityPatch


def map_module_name(chapter_name: str) -> str:
    name = chapter_name.replace("-", "_").replace(".", "").lower()
    return f"campaign_{name}" if name and name[0].isdigit() else name


def _matrix(value: tuple[tuple[str, ...], ...]) -> list[str]:
    return ["    " + " ".join(row) for row in value]


def generate_map_module(
    spec: MapSpec,
    *,
    patches: tuple[CompatibilityPatch, ...] = (),
    base_import: str = "module.campaign.campaign_base",
) -> str:
    if spec.unknown_grid_types or spec.unknown_effects:
        raise ValueError(
            f"Карта {spec.id} не eligible: присутствуют неизвестные mechanics"
        )
    lines = [
        f"from {base_import} import CampaignBase",
        "from module.map.map_base import CampaignMap",
        "",
        f"MAP = CampaignMap({spec.chapter_name!r})",
        f"MAP.shape = {spec.shape!r}",
        f"MAP.camera_data = {list(spec.camera_data)!r}",
        f"MAP.camera_data_spawn_point = {list(spec.camera_spawn_points)!r}",
    ]
    if spec.portals:
        lines.append(
            f"MAP.portal_data = {[(item.source, item.target) for item in spec.portals]!r}"
        )
    lines.extend(['MAP.map_data = """', *_matrix(spec.map_data), '"""'])
    if spec.map_data_loop:
        lines.extend(['MAP.map_data_loop = """', *_matrix(spec.map_data_loop), '"""'])
    lines.extend(
        [
            'MAP.weight_data = """',
            *["    " + " ".join("50" for _ in row) for row in spec.map_data],
            '"""',
        ]
    )
    if spec.land_based:
        lines.append(f"MAP.land_based_data = {list(spec.land_based)!r}")
    lines.append(f"MAP.spawn_data = {list(spec.spawn_data)!r}")
    if spec.spawn_data_loop:
        lines.append(f"MAP.spawn_data_loop = {list(spec.spawn_data_loop)!r}")
    lines.extend(
        [
            "",
            "class Config:",
            "    # Только факты карты; runtime policy задаётся отдельно.",
        ]
    )
    factual = {
        "MAP_HAS_MAP_STORY": spec.has_story,
        "MAP_HAS_FLEET_STEP": spec.has_fleet_step,
        "MAP_HAS_AMBUSH": spec.has_ambush,
        "MAP_HAS_MYSTERY": spec.has_mystery,
        "MAP_HAS_PORTAL": bool(spec.portals),
        "MAP_HAS_LAND_BASED": bool(spec.land_based),
        "MAP_HAS_SIREN": bool(spec.siren_templates),
        "MAP_HAS_MOVABLE_ENEMY": bool(spec.movable_enemy_turns),
        "STAR_REQUIRE_1": spec.star_requirements[0],
        "STAR_REQUIRE_2": spec.star_requirements[1],
        "STAR_REQUIRE_3": spec.star_requirements[2],
    }
    for key, value in factual.items():
        lines.append(f"    {key} = {value!r}")
    if spec.siren_templates:
        lines.append(f"    MAP_SIREN_TEMPLATE = {list(spec.siren_templates)!r}")
        lines.append(f"    MOVABLE_ENEMY_TURN = {tuple(spec.movable_enemy_turns)!r}")
    for patch in patches:
        lines.append(f"    # Compatibility patch {patch.id}: {patch.reason}")
        for key, value in patch.config:
            lines.append(f"    {key} = {value!r}")
    lines.extend(
        [
            "",
            "class Campaign(CampaignBase):",
            "    MAP = MAP",
            "",
            f"    def battle_{spec.boss_refresh}(self):",
            (
                "        return self.fleet_boss.clear_boss()"
                if spec.boss_refresh >= 5
                else "        return self.clear_boss()"
            ),
            "",
        ]
    )
    result = "\n".join(lines) + "\n"
    ast.parse(result)
    return result


def write_map_module(
    path: Path | str, content: str, *, overwrite: bool = False
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
