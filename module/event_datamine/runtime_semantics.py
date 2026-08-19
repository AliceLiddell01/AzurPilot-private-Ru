"""Типизированные runtime-семантики generated Event-карт.

Модуль описывает только декларативные значения, которые можно безопасно
получить из проверенного runtime evidence. Произвольный Python-код и имена
Config-полей через policy не принимаются.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_NODE = re.compile(r"[A-Z]+[1-9][0-9]*")
_ENEMY_FILTER = re.compile(r"[123][LMEC](?:\s*>\s*[123][LMEC])*")
_ALLOWED_CORNERS = frozenset(
    {
        "upper-left",
        "upper-right",
        "bottom-left",
        "bottom-right",
        "upper",
        "bottom",
        "left",
        "right",
    }
)


@dataclass(frozen=True)
class CameraCalibrationPolicy:
    camera_data: tuple[str, ...]
    spawn_points: tuple[str, ...]


@dataclass(frozen=True)
class LinePeaksPolicy:
    height: tuple[float, float]
    prominence: float
    distance: float
    width: tuple[float, float] | None = None
    wlen: float | None = None

    def as_config(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "height": self.height,
            "prominence": self.prominence,
            "distance": self.distance,
        }
        if self.width is not None:
            result["width"] = self.width
        if self.wlen is not None:
            result["wlen"] = self.wlen
        return result


@dataclass(frozen=True)
class SwipeCalibrationPolicy:
    adb: tuple[float, float]
    minitouch: tuple[float, float]
    maatouch: tuple[float, float]


@dataclass(frozen=True)
class DetectorCalibrationPolicy:
    internal_lines: LinePeaksPolicy
    edge_lines: LinePeaksPolicy
    swipe: SwipeCalibrationPolicy
    walk_use_current_fleet: bool | None = None
    ensure_edge_insight_corner: str | None = None

    def config_items(self) -> tuple[tuple[str, Any], ...]:
        result: list[tuple[str, Any]] = [
            ("INTERNAL_LINES_FIND_PEAKS_PARAMETERS", self.internal_lines.as_config()),
            ("EDGE_LINES_FIND_PEAKS_PARAMETERS", self.edge_lines.as_config()),
            ("MAP_SWIPE_MULTIPLY", self.swipe.adb),
            ("MAP_SWIPE_MULTIPLY_MINITOUCH", self.swipe.minitouch),
            ("MAP_SWIPE_MULTIPLY_MAATOUCH", self.swipe.maatouch),
        ]
        if self.walk_use_current_fleet is not None:
            result.append(("MAP_WALK_USE_CURRENT_FLEET", self.walk_use_current_fleet))
        if self.ensure_edge_insight_corner is not None:
            result.append(
                ("MAP_ENSURE_EDGE_INSIGHT_CORNER", self.ensure_edge_insight_corner)
            )
        return tuple(result)


@dataclass(frozen=True)
class SirenFilterStepPolicy:
    battle: int
    preserve: int


@dataclass(frozen=True)
class BattlePlanPolicy:
    enemy_filter: str
    siren_filter_steps: tuple[SirenFilterStepPolicy, ...]


def _reject_unknown(
    raw: Mapping[str, Any], allowed: set[str], label: str, error_type: type[ValueError]
) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise error_type(f"{label} содержит неизвестные поля: {sorted(unknown)}")


def _number(value: Any, label: str, error_type: type[ValueError]) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise error_type(f"{label} должен быть числом")
    result = float(value)
    if not math.isfinite(result):
        raise error_type(f"{label} должен быть конечным числом")
    return result


def _pair(
    value: Any,
    label: str,
    error_type: type[ValueError],
    *,
    positive: bool = True,
) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) != 2:
        raise error_type(f"{label} должен содержать ровно два числа")
    result = (
        _number(value[0], f"{label}[0]", error_type),
        _number(value[1], f"{label}[1]", error_type),
    )
    if result[0] > result[1]:
        raise error_type(f"{label} должен быть упорядочен по возрастанию")
    if positive and result[0] <= 0:
        raise error_type(f"{label} должен содержать положительные числа")
    return result


def _nodes(value: Any, label: str, error_type: type[ValueError]) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise error_type(f"{label} должен быть списком узлов карты")
    result: list[str] = []
    for raw in value:
        node = str(raw or "").strip().upper()
        if not _NODE.fullmatch(node):
            raise error_type(f"{label} содержит некорректный узел: {node!r}")
        if node in result:
            raise error_type(f"{label} содержит дублирующий узел: {node!r}")
        result.append(node)
    return tuple(result)


def parse_camera_calibration(
    raw: Any, *, map_id: int, error_type: type[ValueError]
) -> CameraCalibrationPolicy:
    if not isinstance(raw, Mapping):
        raise error_type(f"camera_calibration карты {map_id} должна быть JSON object")
    _reject_unknown(
        raw,
        {"camera_data", "spawn_points"},
        f"camera_calibration карты {map_id}",
        error_type,
    )
    camera_data = _nodes(
        raw.get("camera_data"), f"camera_calibration.camera_data карты {map_id}", error_type
    )
    spawn_points = _nodes(
        raw.get("spawn_points"), f"camera_calibration.spawn_points карты {map_id}", error_type
    )
    if not camera_data:
        raise error_type(f"camera_calibration карты {map_id} не содержит camera_data")
    if any(node not in camera_data for node in spawn_points):
        raise error_type(
            f"camera_calibration карты {map_id}: spawn_points должны входить в camera_data"
        )
    return CameraCalibrationPolicy(camera_data=camera_data, spawn_points=spawn_points)


def _parse_line_peaks(
    raw: Any, *, label: str, error_type: type[ValueError]
) -> LinePeaksPolicy:
    if not isinstance(raw, Mapping):
        raise error_type(f"{label} должна быть JSON object")
    _reject_unknown(raw, {"height", "width", "prominence", "distance", "wlen"}, label, error_type)
    height = _pair(raw.get("height"), f"{label}.height", error_type, positive=False)
    if not (0 <= height[0] <= height[1] <= 255):
        raise error_type(f"{label}.height должен находиться в диапазоне 0..255")
    width = (
        _pair(raw["width"], f"{label}.width", error_type)
        if "width" in raw
        else None
    )
    prominence = _number(raw.get("prominence"), f"{label}.prominence", error_type)
    distance = _number(raw.get("distance"), f"{label}.distance", error_type)
    if prominence <= 0 or distance <= 0:
        raise error_type(f"{label} требует положительные prominence/distance")
    wlen = (
        _number(raw["wlen"], f"{label}.wlen", error_type)
        if "wlen" in raw
        else None
    )
    if wlen is not None and wlen <= 0:
        raise error_type(f"{label}.wlen должен быть положительным")
    return LinePeaksPolicy(
        height=height,
        width=width,
        prominence=prominence,
        distance=distance,
        wlen=wlen,
    )


def parse_detector_calibration(
    raw: Any, *, map_id: int, error_type: type[ValueError]
) -> DetectorCalibrationPolicy:
    if not isinstance(raw, Mapping):
        raise error_type(f"detector_calibration карты {map_id} должна быть JSON object")
    _reject_unknown(
        raw,
        {
            "internal_lines",
            "edge_lines",
            "swipe",
            "walk_use_current_fleet",
            "ensure_edge_insight_corner",
        },
        f"detector_calibration карты {map_id}",
        error_type,
    )
    swipe = raw.get("swipe")
    if not isinstance(swipe, Mapping):
        raise error_type(f"detector_calibration.swipe карты {map_id} должна быть JSON object")
    _reject_unknown(swipe, {"adb", "minitouch", "maatouch"}, f"detector_calibration.swipe карты {map_id}", error_type)
    swipe_policy = SwipeCalibrationPolicy(
        adb=_pair(swipe.get("adb"), f"swipe.adb карты {map_id}", error_type),
        minitouch=_pair(
            swipe.get("minitouch"), f"swipe.minitouch карты {map_id}", error_type
        ),
        maatouch=_pair(
            swipe.get("maatouch"), f"swipe.maatouch карты {map_id}", error_type
        ),
    )
    walk = raw.get("walk_use_current_fleet")
    if walk is not None and not isinstance(walk, bool):
        raise error_type(f"walk_use_current_fleet карты {map_id} должен быть bool")
    corner = raw.get("ensure_edge_insight_corner")
    if corner is not None:
        corner = str(corner).strip()
        if corner not in _ALLOWED_CORNERS:
            raise error_type(
                f"Карта {map_id} содержит неподдерживаемый edge corner: {corner!r}"
            )
    return DetectorCalibrationPolicy(
        internal_lines=_parse_line_peaks(
            raw.get("internal_lines"),
            label=f"detector_calibration.internal_lines карты {map_id}",
            error_type=error_type,
        ),
        edge_lines=_parse_line_peaks(
            raw.get("edge_lines"),
            label=f"detector_calibration.edge_lines карты {map_id}",
            error_type=error_type,
        ),
        swipe=swipe_policy,
        walk_use_current_fleet=walk,
        ensure_edge_insight_corner=corner,
    )


def parse_battle_plan(
    raw: Any, *, map_id: int, error_type: type[ValueError]
) -> BattlePlanPolicy:
    if not isinstance(raw, Mapping):
        raise error_type(f"battle_plan карты {map_id} должна быть JSON object")
    _reject_unknown(
        raw,
        {"enemy_filter", "siren_filter_steps"},
        f"battle_plan карты {map_id}",
        error_type,
    )
    enemy_filter = str(raw.get("enemy_filter") or "").strip()
    if not _ENEMY_FILTER.fullmatch(enemy_filter):
        raise error_type(f"Карта {map_id} содержит некорректный enemy_filter")
    raw_steps = raw.get("siren_filter_steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise error_type(f"battle_plan карты {map_id} требует siren_filter_steps")
    steps: list[SirenFilterStepPolicy] = []
    seen: set[int] = set()
    for raw_step in raw_steps:
        if not isinstance(raw_step, Mapping):
            raise error_type(f"battle_plan карты {map_id} содержит некорректный step")
        _reject_unknown(raw_step, {"battle", "preserve"}, f"battle step карты {map_id}", error_type)
        battle = raw_step.get("battle")
        preserve = raw_step.get("preserve")
        if isinstance(battle, bool) or not isinstance(battle, int) or battle < 0:
            raise error_type(f"battle step карты {map_id} содержит некорректный battle")
        if isinstance(preserve, bool) or not isinstance(preserve, int) or preserve < 0:
            raise error_type(f"battle step карты {map_id} содержит некорректный preserve")
        if battle in seen:
            raise error_type(f"battle_plan карты {map_id} дублирует battle_{battle}")
        seen.add(battle)
        steps.append(SirenFilterStepPolicy(battle=battle, preserve=preserve))
    steps.sort(key=lambda item: item.battle)
    return BattlePlanPolicy(enemy_filter=enemy_filter, siren_filter_steps=tuple(steps))
