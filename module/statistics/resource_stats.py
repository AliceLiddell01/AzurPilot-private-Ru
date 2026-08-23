"""Проекция снимков ресурсов из production PostgreSQL."""

from __future__ import annotations

from typing import Any

from module.application.resource_fields import RESOURCE_NAME_MAP
from module.application.runtime_storage import get_runtime_storage


def record_resource_snapshot(instance: str, resources: dict[str, Any]) -> bool:
    """Записать один типизированный снимок ресурсов."""

    normalized = {
        target: None if resources.get(source) is None else int(resources[source])
        for source, target in RESOURCE_NAME_MAP.items()
        if source in resources
    }
    return get_runtime_storage().record_resource_snapshot(instance, normalized)


def get_resource_timeline(
    instance: str = "default", limit: int = 500
) -> list[dict[str, Any]]:
    """Вернуть детерминированную временную шкалу ресурсов."""

    snapshots = get_runtime_storage().resource_timeline(instance, limit=limit)
    return [
        {
            "ts": snapshot.observed_at.isoformat()
            if snapshot.observed_at is not None
            else snapshot.legacy_timestamp_text,
            **{
                name: getattr(snapshot, name)
                for name in (
                    "oil",
                    "coin",
                    "gem",
                    "pt",
                    "cube",
                    "core",
                    "medal",
                    "merit",
                    "guild_coin",
                    "action_point",
                    "yellow_coin",
                    "purple_coin",
                )
            },
        }
        for snapshot in snapshots
    ]


__all__ = ["record_resource_snapshot", "get_resource_timeline"]
