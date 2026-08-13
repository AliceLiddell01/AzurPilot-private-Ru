"""Чистый parser/validator карт ShareCfg, не выполняющий запись файлов."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from module.base.utils import location2node, node2location
from module.event_datamine.model import MapSpec, PortalSpec, ValidationFinding
from module.map.utils import camera_2d, camera_spawn_point, get_map_active_area

GRID_TOKENS = {
    0: "--",
    1: "SP",
    2: "MM",
    3: "MA",
    4: "Me",
    6: "ME",
    8: "MB",
    12: "MS",
    16: "__",
    100: "++",
}
LAND_ROTATIONS = {1: "up", 2: "down", 3: "left", 4: "right"}
KNOWN_SIRENS = {"shengli": "Victorious", "huangjiaxiangshu": "RoyalOak"}


def _values(value: Any) -> list[Any]:
    if isinstance(value, Mapping):
        return [
            value[key]
            for key in sorted(
                value,
                key=lambda item: (0, item) if isinstance(item, int) else (1, str(item)),
            )
        ]
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _at(value: Any, index: int, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(index, default)
    if isinstance(value, (list, tuple)) and 0 <= index < len(value):
        return value[index]
    return default


@dataclass
class _MapEffects:
    enemy_waves: list[Any]
    portals: list[PortalSpec]
    unknown: set[str]


class EventEffectRegistry:
    """Явный registry исключает молчаливое игнорирование новых mechanics."""

    def __init__(self) -> None:
        self._handlers: dict[
            str, Callable[[Any, Mapping[str, Any], _MapEffects], None]
        ] = {
            "enemy": self._enemy,
            "jump": self._jump,
            "jumpsub": self._jump,
        }

    @staticmethod
    def _enemy(effect: Any, _event: Mapping[str, Any], result: _MapEffects) -> None:
        result.enemy_waves.append(_at(effect, 1, {}))

    @staticmethod
    def _jump(effect: Any, event: Mapping[str, Any], result: _MapEffects) -> None:
        address = event.get("address", {})
        source = location2node((int(_at(address, 1, 0)), int(_at(address, 0, 0))))
        target = location2node((int(_at(effect, 2, 0)), int(_at(effect, 1, 0))))
        portal = PortalSpec(source=source, target=target)
        if portal not in result.portals:
            result.portals.append(portal)

    def decode(self, event_ids: Any, templates: Mapping[int, Any]) -> _MapEffects:
        result = _MapEffects([], [], set())
        for event_id in _values(event_ids):
            event = templates.get(int(event_id))
            if not isinstance(event, Mapping):
                result.unknown.add(f"missing:{event_id}")
                continue
            for effect in _values(event.get("effect")):
                kind = str(_at(effect, 0, ""))
                handler = self._handlers.get(kind)
                if handler is None:
                    result.unknown.add(kind or "empty")
                else:
                    handler(effect, event, result)
        return result


class MapCompiler:
    def __init__(
        self,
        chapters: Mapping[int, Any],
        chapter_loops: Mapping[int, Any],
        map_event_list: Mapping[int, Any],
        map_event_templates: Mapping[int, Any],
        expeditions: Mapping[int, Any],
    ) -> None:
        self.chapters = chapters
        self.chapter_loops = chapter_loops
        self.map_event_list = map_event_list
        self.map_event_templates = map_event_templates
        self.expeditions = expeditions
        self.effects = EventEffectRegistry()

    @staticmethod
    def _spawn(
        row: Mapping[str, Any],
        enemies: list[Any],
        findings: list[ValidationFinding],
        path: str,
    ) -> tuple[dict[str, int], ...]:
        boss = int(row.get("boss_refresh", 0) or 0)

        def wave_counts(value: Any, label: str) -> list[tuple[int, int]]:
            if isinstance(value, Mapping):
                entries = value.items()
            elif isinstance(value, (list, tuple)):
                entries = enumerate(value)
            elif value in (None, ""):
                return []
            else:
                findings.append(
                    ValidationFinding(
                        "spawn_data_invalid",
                        "error",
                        f"{label} имеет неподдерживаемую форму",
                        path,
                    )
                )
                return []
            result: list[tuple[int, int]] = []
            for wave, count in entries:
                try:
                    wave_number = int(wave)
                    count_number = int(count or 0)
                except TypeError, ValueError, OverflowError:
                    findings.append(
                        ValidationFinding(
                            "spawn_data_invalid",
                            "error",
                            f"{label} содержит нечисловую wave/count запись",
                            path,
                        )
                    )
                    continue
                if wave_number < 0 or count_number < 0:
                    findings.append(
                        ValidationFinding(
                            "spawn_data_invalid",
                            "error",
                            f"{label} содержит отрицательную wave/count запись",
                            path,
                        )
                    )
                    continue
                result.append((wave_number, count_number))
            return result

        groups = {
            "enemy": wave_counts(row.get("enemy_refresh"), "enemy_refresh"),
            "siren": wave_counts(row.get("ai_refresh"), "ai_refresh"),
            "mystery": wave_counts(row.get("box_refresh"), "box_refresh"),
        }
        elite = wave_counts(row.get("elite_refresh"), "elite_refresh")
        active_elite = (
            elite if "".join(str(count) for _, count in elite) != "100" else []
        )
        max_wave = max(
            [
                boss,
                *(
                    wave
                    for values in groups.values()
                    for wave, count in values
                    if count > 0
                ),
                *(wave for wave, count in active_elite if count > 0),
            ]
        )
        rows = [{"battle": index} for index in range(max_wave + 1)]
        for label, values in groups.items():
            for wave, count in values:
                if count > 0:
                    rows[wave][label] = rows[wave].get(label, 0) + count
        for wave, count in active_elite:
            if count > 0:
                rows[wave]["enemy"] = rows[wave].get("enemy", 0) + count
        for wave, entries in enumerate(enemies):
            count = len(_values(entries))
            if count:
                while wave >= len(rows):
                    rows.append({"battle": len(rows)})
                rows[wave]["enemy"] = rows[wave].get("enemy", 0) + count
        while boss >= len(rows):
            rows.append({"battle": len(rows)})
        rows[boss]["boss"] = 1
        return tuple(rows)

    @staticmethod
    def _grid(
        row: Mapping[str, Any], enemies: list[Any]
    ) -> tuple[
        tuple[tuple[str, ...], ...], dict[tuple[int, int], str], tuple[int, ...]
    ]:
        grids = _values(row.get("grids"))
        if not grids:
            return (), {}, ()
        min_y = min(int(_at(grid, 0, 0)) for grid in grids)
        min_x = min(int(_at(grid, 1, 0)) for grid in grids)
        parsed: dict[tuple[int, int], str] = {}
        unknown: set[int] = set()
        for grid in grids:
            location = (int(_at(grid, 1, 0)) - min_x, int(_at(grid, 0, 0)) - min_y)
            grid_type = int(_at(grid, 3, 0) or 0)
            if not bool(_at(grid, 2, False)):
                token = "++"
            else:
                token = GRID_TOKENS.get(grid_type, "??")
                if token == "??":
                    unknown.add(grid_type)
            parsed[location] = token
        for wave in enemies:
            for enemy in _values(wave):
                position = _at(enemy, 1, {})
                parsed[
                    (int(_at(position, 1, 0)) - min_x, int(_at(position, 0, 0)) - min_y)
                ] = "ME"
        max_x = max(position[0] for position in parsed)
        max_y = max(position[1] for position in parsed)
        matrix = tuple(
            tuple(parsed.get((x, y), "??") for x in range(max_x + 1))
            for y in range(max_y + 1)
        )
        return matrix, parsed, tuple(sorted(unknown))

    def compile(self, map_id: int) -> tuple[MapSpec | None, list[ValidationFinding]]:
        findings: list[ValidationFinding] = []
        row = self.chapters.get(map_id)
        if not isinstance(row, Mapping):
            return None, [
                ValidationFinding(
                    "map_missing",
                    "error",
                    f"Карта {map_id} отсутствует",
                    f"maps.{map_id}",
                )
            ]
        loop = self.chapter_loops.get(map_id)
        event_row = self.map_event_list.get(map_id, {})
        normal_effects = self.effects.decode(
            event_row.get("event_list", {}) if isinstance(event_row, Mapping) else {},
            self.map_event_templates,
        )
        loop_effects = self.effects.decode(
            event_row.get("event_list_loop", {})
            if isinstance(event_row, Mapping)
            else {},
            self.map_event_templates,
        )
        matrix, parsed, unknown_grids = self._grid(row, normal_effects.enemy_waves)
        loop_matrix = None
        if isinstance(loop, Mapping):
            parsed_loop, _, loop_unknown = self._grid(loop, loop_effects.enemy_waves)
            unknown_grids = tuple(sorted(set(unknown_grids) | set(loop_unknown)))
            if parsed_loop != matrix:
                loop_matrix = parsed_loop
        if unknown_grids:
            findings.append(
                ValidationFinding(
                    "unknown_grid",
                    "error",
                    f"Неизвестные grid type: {unknown_grids}",
                    f"maps.{map_id}.map_data",
                )
            )
        unknown_effects = tuple(sorted(normal_effects.unknown | loop_effects.unknown))
        if unknown_effects:
            findings.append(
                ValidationFinding(
                    "unknown_effect",
                    "error",
                    f"Неподдерживаемые map effects: {unknown_effects}",
                    f"maps.{map_id}.effects",
                )
            )
        if any(token == "??" for line in matrix for token in line):
            findings.append(
                ValidationFinding(
                    "map_topology_hole",
                    "error",
                    "Прямоугольная область карты содержит неописанную клетку",
                    f"maps.{map_id}.map_data",
                )
            )

        if not matrix:
            findings.append(
                ValidationFinding(
                    "empty_map", "error", "Карта не содержит grid", f"maps.{map_id}"
                )
            )
            return None, findings
        shape = location2node((len(matrix[0]) - 1, len(matrix) - 1))
        flat_tokens = {token for line in matrix for token in line}
        if "SP" not in flat_tokens:
            findings.append(
                ValidationFinding(
                    "spawn_point_missing",
                    "error",
                    "Карта не содержит стартовую клетку SP",
                    f"maps.{map_id}.map_data",
                )
            )
        if "MB" not in flat_tokens:
            findings.append(
                ValidationFinding(
                    "boss_grid_missing",
                    "error",
                    "Карта не содержит boss-клетку MB",
                    f"maps.{map_id}.map_data",
                )
            )
        for portal in normal_effects.portals:
            for endpoint, label in (
                (portal.source, "source"),
                (portal.target, "target"),
            ):
                x, y = node2location(endpoint)
                if not (0 <= x < len(matrix[0]) and 0 <= y < len(matrix)):
                    findings.append(
                        ValidationFinding(
                            "portal_out_of_bounds",
                            "error",
                            f"Portal {label} {endpoint} находится вне shape {shape}",
                            f"maps.{map_id}.portals",
                        )
                    )
        active = get_map_active_area(parsed)
        cameras = camera_2d(active, sight=(-3, -1, 3, 2))
        camera_nodes = tuple(
            location2node(tuple(int(v) for v in location)) for location in cameras
        )
        spawn_nodes = tuple(
            location2node(tuple(int(v) for v in location))
            for location in camera_spawn_point(
                cameras, sp_list=[key for key, value in parsed.items() if value == "SP"]
            )
        )

        land_based: list[tuple[str, str]] = []
        for land in _values(row.get("land_based")):
            rotation = int(_at(land, 2, 0) or 0)
            if rotation not in LAND_ROTATIONS:
                findings.append(
                    ValidationFinding(
                        "unknown_land_rotation",
                        "error",
                        f"Неизвестный land rotation {rotation}",
                        f"maps.{map_id}.land_based",
                    )
                )
                continue
            land_based.append(
                (
                    location2node((int(_at(land, 1, 0)), int(_at(land, 0, 0)))),
                    LAND_ROTATIONS[rotation],
                )
            )

        sirens: list[str] = []
        turns: set[int] = set()
        expedition_ids = _values(row.get("ai_expedition_list"))
        if isinstance(loop, Mapping):
            expedition_ids += _values(loop.get("ai_expedition_list"))
        for expedition_id in expedition_ids:
            if int(expedition_id) == 1:
                continue
            expedition = self.expeditions.get(int(expedition_id), {})
            icon = (
                str(expedition.get("icon") or expedition_id)
                if isinstance(expedition, Mapping)
                else str(expedition_id)
            )
            name = KNOWN_SIRENS.get(icon, icon)
            if name not in sirens:
                sirens.append(name)
            turns.add(
                int(expedition.get("ai_mov", 2) or 2)
                if isinstance(expedition, Mapping)
                else 2
            )

        spawn = self._spawn(
            row, normal_effects.enemy_waves, findings, f"maps.{map_id}.spawn_data"
        )
        spawn_loop = (
            self._spawn(
                loop,
                loop_effects.enemy_waves,
                findings,
                f"maps.{map_id}.spawn_data_loop",
            )
            if isinstance(loop, Mapping)
            else None
        )
        if spawn_loop == spawn:
            spawn_loop = None
        spec = MapSpec(
            id=map_id,
            chapter_name=str(row.get("chapter_name") or "").replace("–", "-"),
            name=str(row.get("name") or ""),
            shape=shape,
            map_data=matrix,
            map_data_loop=loop_matrix,
            spawn_data=spawn,
            spawn_data_loop=spawn_loop,
            camera_data=camera_nodes,
            camera_spawn_points=spawn_nodes,
            boss_refresh=int(row.get("boss_refresh", 0) or 0),
            siren_templates=tuple(sirens),
            movable_enemy_turns=tuple(sorted(turns)),
            land_based=tuple(land_based),
            portals=tuple(normal_effects.portals),
            star_requirements=tuple(
                int(row.get(f"star_require_{index}", index) or 0)
                for index in range(1, 4)
            ),
            has_story=bool(_values(row.get("story_refresh_boss"))),
            has_fleet_step=bool(row.get("is_limit_move")),
            has_ambush=bool(row.get("is_ambush")) or bool(row.get("is_air_attack")),
            has_mystery=any(item.get("mystery", 0) for item in spawn),
            unknown_grid_types=unknown_grids,
            unknown_effects=unknown_effects,
        )
        return spec, findings
