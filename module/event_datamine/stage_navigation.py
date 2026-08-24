"""Проверяемая policy навигации generated Event-этапов.

Файл ``navigation.json`` лежит рядом с generated package и описывает только
семантику переходов и расположения этапов в UI. Backend знает схему policy и
допустимые типы маршрутов, но не имена этапов конкретного события.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from module.event_datamine.artifact import canonical_json
from module.event_datamine.runtime_policy import (
    GENERATED_EVENT_ROOT,
    EventRuntimePolicyError,
    load_generated_runtime_policy,
    map_runtime_policy,
    runtime_map_policies,
)

STAGE_NAVIGATION_SCHEMA_VERSION = 1
_SAFE_PACKAGE_PART = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SAFE_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_ALLOWED_DIFFICULTIES = frozenset({"normal", "hard"})
_ALLOWED_UI_PAGES = frozenset({"campaign", "event", "sp"})
_ALLOWED_UI_MODES = frozenset({"normal", "hard", "ex", "combat", "story"})
_ALLOWED_UI_ASIDES = frozenset({"part1", "part2", "sp", "ex"})
_ALLOWED_TOP_LEVEL = {
    "stage_navigation_schema_version",
    "generated_package",
    "event_id",
    "runtime_policy_digest",
    "stages",
    "digest",
}
_ALLOWED_STAGE = {
    "module",
    "map_id",
    "chapter_name",
    "auto_next",
    "difficulty",
    "ui_page",
    "ui_mode",
    "ui_aside",
    "ui_chapter_index",
    "entrance_names",
}


class EventStageNavigationError(ValueError):
    """Policy навигации generated Event-этапов не прошла проверку."""


@dataclass(frozen=True)
class StageNavigationPolicy:
    """Семантика одного generated-этапа без event-specific логики в backend."""

    module: str
    map_id: int
    chapter_name: str
    auto_next: str | None = None
    difficulty: str | None = None
    ui_page: str | None = None
    ui_mode: str | None = None
    ui_aside: str | None = None
    ui_chapter_index: int | None = None
    entrance_names: tuple[str, ...] = ()

    @property
    def has_ui_route(self) -> bool:
        """Есть ли достаточно данных, чтобы заменить legacy UI-маршрутизацию."""

        return bool(self.ui_page and self.entrance_names)


def stage_navigation_digest(data: Mapping[str, Any]) -> str:
    clean = dict(data)
    clean.pop("digest", None)
    return sha256(canonical_json(clean).encode("utf-8")).hexdigest()


def _package_path(parts: tuple[str, ...], root: Path | str) -> Path:
    if not parts or any(not _SAFE_PACKAGE_PART.fullmatch(part) for part in parts):
        raise EventStageNavigationError("Некорректный generated campaign package")
    base = Path(root).resolve()
    target = base.joinpath(*parts, "navigation.json").resolve()
    if base not in target.parents:
        raise EventStageNavigationError(
            "Navigation-policy вышла за пределы generated_event"
        )
    return target


def _reject_unknown(mapping: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(mapping) - allowed
    if unknown:
        raise EventStageNavigationError(
            f"{label} содержит неизвестные поля: {sorted(unknown)}"
        )


def _required_text(raw: Mapping[str, Any], key: str, *, label: str) -> str:
    value = str(raw.get(key) or "").strip()
    if not value:
        raise EventStageNavigationError(f"{label} не содержит {key}")
    return value


def _optional_choice(
    raw: Mapping[str, Any],
    key: str,
    allowed: frozenset[str],
    *,
    label: str,
) -> str | None:
    if key not in raw:
        return None
    value = str(raw[key] or "").strip()
    if value not in allowed:
        raise EventStageNavigationError(
            f"{label}.{key} содержит неподдерживаемое значение: {value!r}"
        )
    return value


def _parse_stage(raw: Any) -> StageNavigationPolicy:
    if not isinstance(raw, Mapping):
        raise EventStageNavigationError(
            "Элемент stages navigation-policy должен быть JSON object"
        )
    _reject_unknown(raw, _ALLOWED_STAGE, "Этап navigation-policy")

    module = _required_text(raw, "module", label="Этап navigation-policy")
    if not _SAFE_MODULE.fullmatch(module):
        raise EventStageNavigationError(
            f"Этап navigation-policy содержит некорректный module: {module!r}"
        )

    map_id = raw.get("map_id")
    if isinstance(map_id, bool) or not isinstance(map_id, int) or map_id <= 0:
        raise EventStageNavigationError(
            f"Этап {module!r} содержит некорректный map_id: {map_id!r}"
        )

    chapter_name = _required_text(
        raw,
        "chapter_name",
        label=f"Этап {module!r}",
    )

    auto_next = None
    if "auto_next" in raw:
        auto_next = str(raw["auto_next"] or "").strip()
        if not _SAFE_MODULE.fullmatch(auto_next):
            raise EventStageNavigationError(
                f"Этап {module!r} содержит некорректный auto_next: {auto_next!r}"
            )

    chapter_index = None
    if "ui_chapter_index" in raw:
        chapter_index = raw["ui_chapter_index"]
        if (
            isinstance(chapter_index, bool)
            or not isinstance(chapter_index, int)
            or chapter_index <= 0
        ):
            raise EventStageNavigationError(
                f"Этап {module!r} содержит некорректный ui_chapter_index"
            )

    entrance_names: tuple[str, ...] = ()
    if "entrance_names" in raw:
        values = raw["entrance_names"]
        if not isinstance(values, list) or not values:
            raise EventStageNavigationError(
                f"Этап {module!r} требует непустой список entrance_names"
            )
        normalized: list[str] = []
        folded: set[str] = set()
        for value in values:
            name = str(value or "").strip()
            if not name:
                raise EventStageNavigationError(
                    f"Этап {module!r} содержит пустое имя входа"
                )
            key = name.casefold()
            if key in folded:
                raise EventStageNavigationError(
                    f"Этап {module!r} содержит дублирующее имя входа {name!r}"
                )
            folded.add(key)
            normalized.append(name)
        entrance_names = tuple(normalized)

    return StageNavigationPolicy(
        module=module,
        map_id=map_id,
        chapter_name=chapter_name,
        auto_next=auto_next,
        difficulty=_optional_choice(
            raw,
            "difficulty",
            _ALLOWED_DIFFICULTIES,
            label=f"Этап {module!r}",
        ),
        ui_page=_optional_choice(
            raw,
            "ui_page",
            _ALLOWED_UI_PAGES,
            label=f"Этап {module!r}",
        ),
        ui_mode=_optional_choice(
            raw,
            "ui_mode",
            _ALLOWED_UI_MODES,
            label=f"Этап {module!r}",
        ),
        ui_aside=_optional_choice(
            raw,
            "ui_aside",
            _ALLOWED_UI_ASIDES,
            label=f"Этап {module!r}",
        ),
        ui_chapter_index=chapter_index,
        entrance_names=entrance_names,
    )


def _validate_graph(stages: Mapping[str, StageNavigationPolicy]) -> None:
    edges: dict[str, str] = {}
    for module, stage in stages.items():
        if stage.auto_next is None:
            continue
        target = stage.auto_next.casefold()
        if target not in stages:
            raise EventStageNavigationError(
                f"Этап {stage.module!r} ссылается на неизвестный auto_next "
                f"{stage.auto_next!r}"
            )
        if target == module:
            raise EventStageNavigationError(
                f"Этап {stage.module!r} не может ссылаться на себя"
            )
        edges[module] = target

    for start in edges:
        current = start
        seen: set[str] = set()
        while current in edges:
            if current in seen:
                raise EventStageNavigationError(
                    "Navigation-policy содержит цикл автопродвижения"
                )
            seen.add(current)
            current = edges[current]


def _validate_runtime_binding(
    data: Mapping[str, Any],
    stages: Mapping[str, StageNavigationPolicy],
    *,
    package_parts: tuple[str, ...],
    root: Path | str = GENERATED_EVENT_ROOT,
) -> None:
    try:
        runtime = load_generated_runtime_policy(package_parts, root=root)
    except EventRuntimePolicyError as exc:
        raise EventStageNavigationError(
            "Связанная runtime-policy generated package повреждена"
        ) from exc
    if runtime is None:
        raise EventStageNavigationError(
            "Navigation-policy требует связанную runtime-policy"
        )

    event_id = str(data.get("event_id") or "").strip()
    if str(runtime.get("event_id") or "").strip() != event_id:
        raise EventStageNavigationError(
            "Navigation-policy и runtime-policy относятся к разным Event identity"
        )
    expected_runtime_digest = str(data.get("runtime_policy_digest") or "").lower()
    actual_runtime_digest = str(runtime.get("digest") or "").lower()
    if expected_runtime_digest != actual_runtime_digest:
        raise EventStageNavigationError(
            "Navigation-policy привязана к другой версии runtime-policy"
        )

    runtime_maps = runtime_map_policies(runtime)
    if {stage.map_id for stage in stages.values()} != set(runtime_maps):
        raise EventStageNavigationError(
            "Navigation-policy должна покрывать все runtime-карты generated package"
        )

    for stage in stages.values():
        try:
            runtime_stage = map_runtime_policy(
                runtime,
                map_id=stage.map_id,
                chapter_name=stage.chapter_name,
            )
        except EventRuntimePolicyError as exc:
            raise EventStageNavigationError(
                f"Navigation-policy этапа {stage.module!r} не соответствует runtime-policy"
            ) from exc
        if runtime_stage is None:
            raise EventStageNavigationError(
                f"Navigation-policy этапа {stage.module!r} не имеет runtime evidence"
            )


def validate_stage_navigation_policy(
    data: Any,
    *,
    package_parts: tuple[str, ...],
    root: Path | str = GENERATED_EVENT_ROOT,
) -> dict[str, StageNavigationPolicy]:
    if not isinstance(data, Mapping):
        raise EventStageNavigationError("Navigation-policy должна быть JSON object")
    result = dict(data)
    _reject_unknown(result, _ALLOWED_TOP_LEVEL, "Navigation-policy")

    version = result.get("stage_navigation_schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise EventStageNavigationError(
            "stage_navigation_schema_version должен быть int"
        )
    if version != STAGE_NAVIGATION_SCHEMA_VERSION:
        raise EventStageNavigationError(
            "Неподдерживаемая версия navigation-policy"
        )

    package = ".".join(package_parts)
    if str(result.get("generated_package") or "") != package:
        raise EventStageNavigationError(
            "Navigation-policy не соответствует generated package"
        )

    event_id = str(result.get("event_id") or "").strip()
    if not event_id or ":" not in event_id:
        raise EventStageNavigationError(
            "Navigation-policy не содержит Event identity"
        )

    runtime_digest = str(result.get("runtime_policy_digest") or "").lower()
    if not _SHA256.fullmatch(runtime_digest):
        raise EventStageNavigationError(
            "Navigation-policy содержит некорректный runtime_policy_digest"
        )

    raw_stages = result.get("stages")
    if not isinstance(raw_stages, list) or not raw_stages:
        raise EventStageNavigationError(
            "Navigation-policy требует непустой список stages"
        )

    stages: dict[str, StageNavigationPolicy] = {}
    map_ids: set[int] = set()
    chapters: set[str] = set()
    for raw in raw_stages:
        stage = _parse_stage(raw)
        module = stage.module.casefold()
        if module in stages:
            raise EventStageNavigationError(
                f"Navigation-policy дублирует module {stage.module!r}"
            )
        if stage.map_id in map_ids:
            raise EventStageNavigationError(
                f"Navigation-policy дублирует map_id {stage.map_id}"
            )
        chapter = stage.chapter_name.casefold()
        if chapter in chapters:
            raise EventStageNavigationError(
                f"Navigation-policy дублирует chapter_name {stage.chapter_name!r}"
            )
        stages[module] = stage
        map_ids.add(stage.map_id)
        chapters.add(chapter)

    _validate_graph(stages)
    _validate_runtime_binding(
        result,
        stages,
        package_parts=package_parts,
        root=root,
    )

    expected = str(result.get("digest") or "").lower()
    if not _SHA256.fullmatch(expected) or expected != stage_navigation_digest(result):
        raise EventStageNavigationError(
            "Digest navigation-policy не совпадает"
        )
    return stages


def load_generated_stage_navigation(
    package_parts: tuple[str, ...],
    *,
    root: Path | str = GENERATED_EVENT_ROOT,
) -> dict[str, StageNavigationPolicy] | None:
    """Загрузить navigation-policy generated package без сетевых запросов."""

    target = _package_path(package_parts, root)
    if not target.is_file():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EventStageNavigationError(
            f"Не удалось прочитать navigation-policy {target}"
        ) from exc
    return validate_stage_navigation_policy(
        data,
        package_parts=package_parts,
        root=root,
    )


def generated_stage_navigation_for_module(
    module_name: str,
) -> StageNavigationPolicy | None:
    """Разрешить policy конкретного generated-модуля по его package и stem."""

    parts = str(module_name or "").split(".")
    if len(parts) < 4 or parts[:2] != ["campaign", "generated_event"]:
        raise EventStageNavigationError(
            f"Некорректный generated campaign module: {module_name!r}"
        )
    package_parts = tuple(parts[2:-1])
    module = parts[-1]
    if (
        not package_parts
        or any(not _SAFE_PACKAGE_PART.fullmatch(part) for part in package_parts)
        or not _SAFE_MODULE.fullmatch(module)
    ):
        raise EventStageNavigationError(
            f"Некорректный generated campaign module: {module_name!r}"
        )

    stages = load_generated_stage_navigation(package_parts)
    if stages is None:
        return None
    stage = stages.get(module.casefold())
    if stage is None:
        raise EventStageNavigationError(
            f"Navigation-policy не содержит generated module {module!r}"
        )
    return stage
