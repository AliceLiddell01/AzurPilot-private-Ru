"""Явные слои проверяемых патчей совместимости для исключений конкретных карт."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CompatibilityPatch:
    id: str
    event_id: str
    map_id: int
    reason: str
    source_evidence: str
    expected_effect: str
    config: tuple[tuple[str, Any], ...] = ()


PATCHES: tuple[CompatibilityPatch, ...] = tuple(
    CompatibilityPatch(
        id=f"rose-tower-land-code-10-{map_id}",
        event_id="en:5941",
        map_id=map_id,
        reason="ShareCfg land_based code 10 не является направленной береговой батареей",
        source_evidence=f"campaign/event_20250520_cn/{module}.py: проверенная карта не включает MAP.land_based_data",
        expected_effect="Игнорировать только code 10; иные неизвестные коды остаются blocking",
    )
    for map_id, module in (
        (1920004, "b1"),
        (1920005, "b2"),
        (1920006, "b3"),
        (1920024, "d1"),
        (1920025, "d2"),
        (1920026, "d3"),
    )
)


_CURRENT_EVENT_ID = "en:51101"
_CURRENT_EVENT_UPSTREAM_REVISION = "857deff50a845db1e4aa223c58ad5899155990d1"


def _runtime_patch(
    map_id: int,
    module: str,
    *,
    templates: tuple[str, ...],
    small_boss_icon: bool = False,
) -> CompatibilityPatch:
    config: list[tuple[str, Any]] = [("MAP_SIREN_TEMPLATE", list(templates))]
    if small_boss_icon:
        config.append(("MAP_SIREN_HAS_BOSS_ICON_SMALL", True))
    recognition = (
        "маленькой иконке босса"
        if small_boss_icon
        else "проверенным шаблонам " + ", ".join(templates)
    )
    return CompatibilityPatch(
        id=f"depths-astrarium-{module}-siren-recognition",
        event_id=_CURRENT_EVENT_ID,
        map_id=map_id,
        reason=(
            "ShareCfg expedition.icon описывает игровой ресурс и не гарантирует имя "
            "шаблона распознавания AzurPilot"
        ),
        source_evidence=(
            f"wess09/AzurPilot@{_CURRENT_EVENT_UPSTREAM_REVISION} "
            f"campaign/event_20260813_cn/{module}.py"
        ),
        expected_effect=f"Распознавать siren по {recognition}, не угадывая TEMPLATE_SIREN из ShareCfg icon",
        config=tuple(config),
    )


RUNTIME_PATCHES: tuple[CompatibilityPatch, ...] = (
    *(
        _runtime_patch(map_id, module, templates=(), small_boss_icon=True)
        for map_id, module in (
            (2050001, "a1"),
            (2050002, "a2"),
            (2050003, "a3"),
            (2050021, "c1"),
            (2050022, "c2"),
            (2050023, "c3"),
        )
    ),
    *(
        _runtime_patch(
            map_id,
            module,
            templates=("BonhommeRichard_BB", "BonhommeRichard_CV"),
        )
        for map_id, module in (
            (2050004, "b1"),
            (2050005, "b2"),
            (2050006, "b3"),
            (2050024, "d1"),
            (2050025, "d2"),
            (2050026, "d3"),
        )
    ),
    _runtime_patch(2050041, "sp", templates=("BonhommeRichard_SS",)),
    CompatibilityPatch(
        id="depths-astrarium-sp-entry-policy",
        event_id=_CURRENT_EVENT_ID,
        map_id=2050041,
        reason=(
            "SP является одноразовым этапом; этот runtime-факт не выводится "
            "из ShareCfg карты"
        ),
        source_evidence=(
            f"wess09/AzurPilot@{_CURRENT_EVENT_UPSTREAM_REVISION} "
            "campaign/event_20260813_cn/sp.py: MAP_IS_ONE_TIME_STAGE=True"
        ),
        expected_effect="Обрабатывать окно входа SP как одноразовый этап",
        config=(("MAP_IS_ONE_TIME_STAGE", True),),
    ),
)


def patches_for(event_id: str, map_id: int) -> tuple[CompatibilityPatch, ...]:
    """Вернуть только структурные патчи, влияющие на компиляцию EventSpec."""

    return tuple(
        item for item in PATCHES if item.event_id == event_id and item.map_id == map_id
    )


def generation_patches_for(event_id: str, map_id: int) -> tuple[CompatibilityPatch, ...]:
    """Вернуть структурные патчи и патчи выполнения для генерации campaign-модуля."""

    structural = patches_for(event_id, map_id)
    runtime = tuple(
        item
        for item in RUNTIME_PATCHES
        if item.event_id == event_id and item.map_id == map_id
    )
    return structural + runtime
