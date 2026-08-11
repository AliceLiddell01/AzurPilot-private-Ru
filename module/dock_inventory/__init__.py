"""Публичные модели Dock Inventory и offline-каталог canonical identity."""

from module.dock_inventory.catalog import (
    DockCanonicalShip,
    DockIdentityCatalog,
    DockIdentityCatalogError,
    load_dock_identity_catalog,
)
from module.dock_inventory.model import (
    AffinityState,
    CanonicalShipIdentity,
    DockInventoryScanResult,
    DockShipObservation,
    IdentityStatus,
    StarObservation,
)

__all__ = [
    "AffinityState",
    "CanonicalShipIdentity",
    "DockCanonicalShip",
    "DockIdentityCatalog",
    "DockIdentityCatalogError",
    "DockInventoryScanResult",
    "DockShipObservation",
    "IdentityStatus",
    "StarObservation",
    "load_dock_identity_catalog",
]
