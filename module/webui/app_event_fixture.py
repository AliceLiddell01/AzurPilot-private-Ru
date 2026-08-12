"""Temporary historical event fixture for manual acceptance of the new Event UI."""

from __future__ import annotations

from html import escape
from typing import Any, Mapping

from module.webui.event_rose_tower_fixture import (
    ROSE_TOWER_ACTIVITY_ID,
    ROSE_TOWER_MEDAL_GROUP_ID,
    ROSE_TOWER_SHOP_TEMPLATE_ID,
    ROSE_TOWER_SOURCE_KIND,
    ROSE_TOWER_SOURCE_REVISION,
    ROSE_TOWER_SOURCE_TIMEZONE,
    empty_event_plan_without_fixture,
    with_rose_tower_fixture,
)


class EventFixtureMixin:
    """Seed untouched EventPlan state with one source-backed historical fixture."""

    def _event_plan(self):
        """Use the Rose Tower fixture only while the local plan is untouched."""
        return with_rose_tower_fixture(super()._event_plan())

    def _clear_event_plan(self) -> None:
        """Clear explicitly without immediately re-seeding the temporary fixture."""
        if self._event_plan_write(
            empty_event_plan_without_fixture("EN"),
            "Локальный план ивента очищен",
        ):
            self._refresh_event_plan_page()

    def _event_plan_source_label(self, plan: Mapping[str, Any]) -> str:
        source = plan.get("event", {}).get("source", {})
        kind = source.get("kind") if isinstance(source, Mapping) else ""
        if kind == ROSE_TOWER_SOURCE_KIND:
            return "AzurLaneLuaScripts — историческая заглушка"
        if kind == "manual_empty":
            return "Локальный ручной план"
        return super()._event_plan_source_label(plan)

    def _source_badge(self, plan: Mapping[str, Any]) -> str:
        source = plan.get("event", {}).get("source", {})
        kind = source.get("kind") if isinstance(source, Mapping) else ""
        if kind != ROSE_TOWER_SOURCE_KIND:
            return super()._source_badge(plan)

        title = escape(
            "Историческая заглушка из AzurLaneLuaScripts: "
            f"activity={ROSE_TOWER_ACTIVITY_ID}, "
            f"shop={ROSE_TOWER_SHOP_TEMPLATE_ID}, "
            f"medal={ROSE_TOWER_MEDAL_GROUP_ID}, "
            f"rev={ROSE_TOWER_SOURCE_REVISION[:12]}, "
            f"даты EN {ROSE_TOWER_SOURCE_TIMEZONE}. "
            "Данные не применяются к runtime автоматически."
        )
        return (
            '<span class="event-status-pill event-status-warning" '
            f'title="{title}">AzurLaneLuaScripts · заглушка</span>'
        )
