"""Fail-closed automation bridge between the visual Event shop plan and EventShop."""

from __future__ import annotations

from html import escape
from typing import Any, Mapping

from module.webui.app_dependencies import deep_get, logger, pin, put_html, toast, use_scope
from module.webui.app_event_planner import _SHOP_FILTER_PIN
from module.webui.app_types import WebUIMixinBase
from module.webui.event_plan import shop_plan_total
from module.webui.event_shop_bridge import (
    build_event_shop_automation_plan,
    canonical_event_shop_filter_token,
)


class EventShopSafetyMixin(WebUIMixinBase):
    """Keep visual shop edits synchronized without weakening EventShop safety."""

    def _save_shop_item_popup(self) -> None:
        raw_token = str(pin[_SHOP_FILTER_PIN] or "").strip()
        if raw_token and canonical_event_shop_filter_token(raw_token) is None:
            toast(
                "Токен EventShop должен быть одним штатным селектором без «>» "
                "и суффикса «:N». Количество задаётся отдельным полем плана.",
                color="warning",
                duration=8,
            )
            return
        return super()._save_shop_item_popup()

    def _set_event_shop_scheduler(self, enabled: bool) -> bool:
        """Change only EventShop Scheduler.Enable and report whether it was written."""
        try:
            self._save_config(
                {"EventShop.Scheduler.Enable": bool(enabled)},
                self.alas_name,
                self.alas_config,
            )
            self.alas_config.load()
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
            return (
                "Нет безопасного токена EventShop для: "
                + ", ".join(compiled.invalid_items)
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
        """Synchronize a safe plan and pause Scheduler whenever it is not expressible."""
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
            self._save_config(
                {
                    "EventShop.EventShop.UnlockSSRShip": False,
                    "EventShop.EventShop.BuyURShip": 0,
                    "EventShop.EventShop.PresetFilter": "custom",
                    "EventShop.EventShop.CustomFilter": compiled.filter_text,
                },
                self.alas_name,
                self.alas_config,
            )
            self.alas_config.load()
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
        """Persist EventPlan, then auto-sync shop edits only on the EventShop page."""
        saved = super()._event_plan_write(plan, message)
        if not saved:
            return False

        if getattr(self, "_event_plan_active_task", "") == "EventShop":
            self._sync_shop_plan_fail_closed(plan, announce=False)
        return True

    def _render_event_shop_plan(self, config: Mapping[str, Any]) -> None:
        super()._render_event_shop_plan(config)

        plan = self._event_plan()
        total = shop_plan_total(plan)
        compiled = build_event_shop_automation_plan(plan)
        shop_enabled = bool(deep_get(config, "EventShop.Scheduler.Enable", False))
        pt_limit = int(
            deep_get(config, "EventGeneral.EventGeneral.PtLimit", 0) or 0
        )

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
                f'<div><strong>{escape(title)}</strong>'
                f'<small>{escape(detail)}</small></div>'
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

    def _apply_shop_plan_to_automation(self) -> None:
        """Compatibility/manual entry point; the normal UI syncs changes automatically."""
        if not self._event_write_allowed():
            return
        self._sync_shop_plan_fail_closed(
            self._event_plan(),
            announce=True,
        )
        self._refresh_event_plan_page()
