"""Fail-closed WebUI bridge between the visual Event shop plan and EventShop runtime."""

from __future__ import annotations

from typing import Any, Mapping

from module.webui.app_dependencies import deep_get, pin, put_text, toast
from module.webui.app_event_planner import _SHOP_FILTER_PIN
from module.webui.app_types import WebUIMixinBase
from module.webui.event_plan import shop_plan_total
from module.webui.event_shop_bridge import (
    build_event_shop_automation_plan,
    canonical_event_shop_filter_token,
)


class EventShopSafetyMixin(WebUIMixinBase):
    """Validate runtime-facing shop selectors independently from EventPlan storage."""

    def _save_shop_item_popup(self) -> None:
        raw_token = str(pin[_SHOP_FILTER_PIN] or "").strip()
        if raw_token and canonical_event_shop_filter_token(raw_token) is None:
            toast(
                "Токен EventShop должен быть одним штатным селектором без «>» и суффикса «:N». "
                "Количество задаётся отдельным полем плана.",
                color="warning",
                duration=8,
            )
            return
        return super()._save_shop_item_popup()

    def _render_event_shop_plan(self, config: Mapping[str, Any]) -> None:
        super()._render_event_shop_plan(config)
        plan = self._event_plan()
        compiled = build_event_shop_automation_plan(plan)

        invalid_nonempty = []
        invalid_names = set(compiled.invalid_items)
        for item in plan.get("shop_items", []):
            if (
                item.get("name") in invalid_names
                and int(item.get("selected", 0) or 0) > 0
                and str(item.get("filter") or "").strip()
            ):
                invalid_nonempty.append(str(item.get("name")))

        if invalid_nonempty:
            put_text(
                "Некоторые выбранные товары содержат некорректный токен EventShop. "
                "Синхронизация заблокирована: " + ", ".join(invalid_nonempty)
            ).style("font-size: .85rem; opacity: .78;")

        if compiled.conflicts:
            details = "; ".join(
                f"{token}: {', '.join(names)}"
                for token, names in compiled.conflicts.items()
            )
            put_text(
                "Некоторые товары нельзя однозначно выразить одним токеном EventShop с выбранными "
                f"количествами. Синхронизация заблокирована. {details}"
            ).style("font-size: .85rem; opacity: .78;")

        put_text(
            "При синхронизации визуальный план становится источником обычных покупок: "
            "автоматическая покупка неполученного SSR и UR-кораблей отключается, чтобы старые "
            "специальные обработчики не покупали товары вне выбранного плана. Для UR-магазинов "
            "специальную логику пока настраивайте в расширенном блоке EventShop."
        ).style("font-size: .82rem; opacity: .72; margin-top: .5rem;")

        shop_enabled = bool(deep_get(config, "EventShop.Scheduler.Enable", False))
        pt_limit = int(deep_get(config, "EventGeneral.EventGeneral.PtLimit", 0) or 0)
        if shop_enabled and pt_limit > 0:
            put_text(
                "Важно: EventShop расходует текущий баланс PT во время ивента, а автостоп по PT "
                "смотрит именно на текущий баланс. Поэтому одновременно включённый магазин может "
                "отодвигать достижение PT-лимита. Синхронизация магазина намеренно не меняет цель "
                "фарма; используйте кнопку записи цели отдельно только для выбранного вами сценария."
            ).style("font-size: .85rem; opacity: .82; margin-top: .5rem;")

    def _apply_shop_plan_to_automation(self) -> None:
        if not self._event_write_allowed():
            return

        plan = self._event_plan()
        total = shop_plan_total(plan)
        if total <= 0:
            toast("В плане магазина ничего не выбрано", color="warning")
            return

        compiled = build_event_shop_automation_plan(plan)
        if compiled.invalid_items:
            toast(
                "Нельзя безопасно синхронизировать план: отсутствует или некорректен токен EventShop для: "
                + ", ".join(compiled.invalid_items),
                color="warning",
                duration=9,
            )
            return
        if compiled.conflicts:
            details = "; ".join(
                f"{token}: {', '.join(names)}"
                for token, names in compiled.conflicts.items()
            )
            toast(
                "Нельзя безопасно синхронизировать план: один токен EventShop не может однозначно "
                f"выразить выбранные товары и количества. {details}",
                color="warning",
                duration=9,
            )
            return
        if not compiled.filter_text:
            toast("Не удалось построить фильтр магазина", color="warning")
            return

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
        toast(
            "План синхронизирован с фильтром EventShop; цель фарма не изменена, скрытые специальные покупки отключены",
            color="success",
        )
        self._refresh_event_plan_page()