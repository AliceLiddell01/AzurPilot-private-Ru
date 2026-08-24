"""Сканирование состава обычных Formation-флотов без import-time runtime I/O."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

from module.formation.model import (
    SUPPORTED_SURFACE_FLEET_INDICES,
    FleetSelection,
    FormationFleetSide,
    FormationFleetSlotObservation,
    FormationFleetSnapshot,
    validate_surface_fleet_index,
)

if TYPE_CHECKING:
    from module.formation.navigation import FormationFleetController
    from module.formation.scanner import (
        FormationFleetInfoScanner,
        FormationFleetInputError,
        FormationFleetOcrError,
        FormationFleetScanError,
    )

_LAZY_EXPORTS = {
    "FormationFleetController": ("module.formation.navigation", "FormationFleetController"),
    "FormationFleetInfoScanner": ("module.formation.scanner", "FormationFleetInfoScanner"),
    "FormationFleetInputError": ("module.formation.scanner", "FormationFleetInputError"),
    "FormationFleetOcrError": ("module.formation.scanner", "FormationFleetOcrError"),
    "FormationFleetScanError": ("module.formation.scanner", "FormationFleetScanError"),
}


def __getattr__(name: str):
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attribute = target
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value

__all__ = [
    "SUPPORTED_SURFACE_FLEET_INDICES",
    "FleetSelection",
    "FormationFleetController",
    "FormationFleetInfoScanner",
    "FormationFleetInputError",
    "FormationFleetOcrError",
    "FormationFleetScanError",
    "FormationFleetSide",
    "FormationFleetSlotObservation",
    "FormationFleetSnapshot",
    "validate_surface_fleet_index",
]
