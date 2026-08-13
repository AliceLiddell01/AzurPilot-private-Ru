"""Явный слой проверяемых compatibility patches для исключений конкретных карт."""

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


def patches_for(event_id: str, map_id: int) -> tuple[CompatibilityPatch, ...]:
    return tuple(
        item for item in PATCHES if item.event_id == event_id and item.map_id == map_id
    )
