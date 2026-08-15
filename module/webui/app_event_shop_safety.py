"""Fail-closed мост между визуальным планом магазина события и EventShop."""

from __future__ import annotations

from collections.abc import Mapping
from html import escape
from typing import Any

from module.webui.app_dependencies import deep_get, logger, put_html, toast, use_scope
from module.webui.app_event_shop_v2 import EventShopV2Mixin
from module.webui.event_plan import shop_plan_total
from module.webui.event_shop_bridge import (
    build_event_shop_automation_plan,
)


class EventShopSafetyMixin(EventShopV2Mixin):
    """Синхронизировать правки магазина без ослабления защитных ограничений EventShop."""

    def _set_event_shop_scheduler(self, enabled: bool) -> bool:
        """Изменить только EventShop Scheduler.Enable и сообщить об успешной записи."""
        try:
            self._event_config_update({"EventShop.Scheduler.Enable": bool(enabled)})
        except Exception as exc:
            logger.exception(exc)
            toast(
                "Не удалось безопасно изменить состояние EventShop Scheduler. "
                f"Проверьте настройки магазина вручную: {exc}",
                color="error",
                duration=9,
            )
            return False
        return True

    @staticmethod
    def _compiled_shop_problem(compiled) -> str:
        if compiled.invalid_items:
            return "Нет безопасного токена EventShop для: " + ", ".join(
                compiled.invalid_items
            )
        if compiled.conflicts:
            details = "; ".join(
                f"{token}: {', '.join(names)}"
                for token, names in compiled.conflicts.items()
            )
            return (
                "Один токен EventShop неоднозначно описывает выбранные количества. "
                + details
            )
        if not compiled.filter_text:
            return "Не удалось построить фильтр EventShop."
        return ""

    def _sync_shop_plan_fail_closed(
        self,
        plan: Mapping[str, Any],
        *,
        announce: bool,
    ) -> bool:
        """Синхронизировать безопасный план и ставить Scheduler на паузу при неоднозначности."""
        total = shop_plan_total(plan)
        compiled = build_event_shop_automation_plan(plan)

        if total <= 0:
            disabled = self._set_event_shop_scheduler(False)
            if announce:
                toast(
                    "План магазина пуст. EventShop Scheduler отключён, "
                    "чтобы старый фильтр не продолжал покупки.",
                    color="warning" if disabled else "error",
                    duration=8,
                )
            return False

        problem = self._compiled_shop_problem(compiled)
        if problem:
            disabled = self._set_event_shop_scheduler(False)
            if disabled:
                toast(
                    "Автоматизация магазина приостановлена: "
                    f"{problem} Исправьте план и затем при необходимости "
                    "снова включите Scheduler.",
                    color="warning",
                    duration=9,
                )
            return False

        try:
            self._event_config_update(
                {
                    "EventShop.EventShop.UnlockSSRShip": False,
                    "EventShop.EventShop.BuyURShip": 0,
                    "EventShop.EventShop.PresetFilter": "custom",
                    "EventShop.EventShop.CustomFilter": compiled.filter_text,
                }
            )
        except Exception as exc:
            logger.exception(exc)
            paused = self._set_event_shop_scheduler(False)
            if paused:
                detail = "Scheduler отключён в fail-closed режиме."
            else:
                detail = (
                    "Не удалось гарантировать отключение Scheduler — "
                    "проверьте его состояние вручную."
                )
            toast(
                "План сохранён, но безопасно обновить EventShop не удалось. "
                f"{detail} Причина: {exc}",
                color="error",
                duration=10,
            )
            return False

        if announce:
            toast(
                "Фильтр EventShop обновлён автоматически; PT-автостоп не изменён",
                color="success",
            )
        return True

    def _event_plan_write(self, plan: Mapping[str, Any], message: str) -> bool:
        """Сохранить EventPlan и затем привести автоматизацию EventShop в согласованное состояние."""
        saved = super()._event_plan_write(plan, message)
        if not saved:
            return False

        self._sync_shop_plan_fail_closed(plan, announce=False)
        return True

    def _render_event_shop_safety_status(self, config: Mapping[str, Any]) -> None:
        """Отрисовать состояние fail-closed автоматизации для текущего плана магазина."""
        plan = self._event_plan()
        total = shop_plan_total(plan)
        compiled = build_event_shop_automation_plan(plan)
        shop_enabled = bool(deep_get(config, "EventShop.Scheduler.Enable", False))
        pt_limit = int(deep_get(config, "EventGeneral.EventGeneral.PtLimit", 0) or 0)

        problem = self._compiled_shop_problem(compiled) if total > 0 else ""
        if total <= 0:
            tone = "neutral"
            title = "Автоматизация на паузе"
            detail = (
                "План пуст. После удаления последнего выбранного товара "
                "Scheduler отключается автоматически."
            )
        elif problem:
            tone = "warning"
            title = "Автоматизация заблокирована"
            detail = problem
        elif shop_enabled:
            tone = "success"
            title = "Автосинхронизация активна"
            detail = (
                "Безопасный фильтр EventShop обновляется после каждого изменения "
                "плана. Старые SSR/UR-обходы отключены."
            )
        else:
            tone = "neutral"
            title = "План синхронизирован"
            detail = (
                "Фильтр обновляется автоматически, но Scheduler выключен. "
                "Включите его в настройках задачи, когда будете готовы запускать магазин."
            )

        with use_scope("event_shop_safety_status", clear=True):
            put_html(
                f'<div class="event-automation-status event-status-{tone}">'
                '<span class="event-automation-icon"></span>'
                f"<div><strong>{escape(title)}</strong>"
                f"<small>{escape(detail)}</small></div>"
                "</div>"
            )

            if shop_enabled and pt_limit > 0:
                put_html(
                    '<div class="event-inline-note event-inline-note-warning">'
                    "<strong>PT-автостоп и магазин работают по одному текущему балансу.</strong>"
                    "<span>EventShop тратит PT, поэтому включённый магазин может отодвигать "
                    "достижение PT-лимита. Автосинхронизация магазина намеренно никогда "
                    "не меняет цель фарма.</span></div>"
                )

    def _render_event_shop_plan(self, config: Mapping[str, Any]) -> None:
        """Сохранить safety-статус при частичной перерисовке V2-плана."""
        super()._render_event_shop_plan(config)
        self._render_event_shop_safety_status(config)

    def _render_event_shop_layout(self, *, task, group_map, config) -> None:
        """Добавить safety-статус уже на первом V2-рендере страницы."""
        super()._render_event_shop_layout(
            task=task,
            group_map=group_map,
            config=config,
        )
        with use_scope("group_EventShopPlan"):
            self._render_event_shop_safety_status(config)

    def _apply_shop_plan_to_automation(self) -> None:
        """Ручная точка совместимости; обычный UI синхронизирует изменения автоматически."""
        if not self._event_write_allowed():
            return
        self._sync_shop_plan_fail_closed(
            self._event_plan(),
            announce=True,
        )
        self._refresh_event_plan_page()
