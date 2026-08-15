"""Операции только над пользовательской политикой Event UI."""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import partial
from hashlib import sha256
from threading import RLock
from typing import Any

from module.webui.app_dependencies import (
    close_popup,
    logger,
    pin,
    popup,
    put_button,
    put_input,
    put_row,
    run_js,
    toast,
    use_scope,
)
from module.webui.app_helpers import is_demo_mode
from module.webui.app_types import WebUIMixinBase
from module.webui.event_config import update_event_config
from module.webui.event_plan import (
    load_event_plan,
    selected_shop_filter_tokens,
    selected_shop_items_missing_filter,
    shop_plan_total,
)
from module.webui.event_shop_priority import update_event_shop_target_state
from module.webui.event_source import (
    load_event_user_state,
    save_event_user_state,
    user_state_from_plan,
)

_SHOP_SELECTED_PIN = "event_plan_shop_selected"
_EVENT_PLAN_MUTATION_LOCK = RLock()
_STALE_EVENT_PLAN = object()
_UNCHANGED_EVENT_PLAN = object()


def _plural_positions(count: int) -> str:
    """Вернуть корректную русскую форму счётчика позиций."""
    value = abs(int(count))
    tail = value % 100
    if 11 <= tail <= 14:
        form = "позиций"
    elif value % 10 == 1:
        form = "позиция"
    elif value % 10 in (2, 3, 4):
        form = "позиции"
    else:
        form = "позиций"
    return f"{count} {form}"


class EventPlannerMixin(WebUIMixinBase):
    """Сохраняет прогресс и выбранные значения, не изменяя факты datamine."""

    def _event_plan(self) -> dict[str, Any]:
        return load_event_plan(self.alas_name)

    def _event_plan_write(self, plan: Mapping[str, Any], message: str) -> bool:
        if is_demo_mode():
            toast(
                "В демонстрационном режиме изменение плана ивента отключено.",
                color="warning",
            )
            return False
        try:
            with _EVENT_PLAN_MUTATION_LOCK:
                previous = load_event_user_state(self.alas_name)
                save_event_user_state(
                    self.alas_name,
                    user_state_from_plan(plan, previous),
                )
        except Exception as exc:
            logger.exception(exc)
            toast(f"Не удалось сохранить политику ивента: {exc}", color="error")
            return False
        if message:
            toast(message, color="success")
        return True

    def _event_plan_mutate(self, mutation, message: str) -> bool:
        with _EVENT_PLAN_MUTATION_LOCK:
            plan = self._event_plan()
            result = mutation(plan)
            if result is _STALE_EVENT_PLAN:
                self._stale_plan_message()
                return False
            if result is _UNCHANGED_EVENT_PLAN:
                return False
            return self._event_plan_write(plan, message)

    def _event_config_update(self, updates: Mapping[str, Any]) -> None:
        update_event_config(self.alas_config, self.alas_name, updates)
        self.alas_config.load()

    def _refresh_event_plan_page(self) -> None:
        task = getattr(self, "_event_plan_active_task", "")
        if task == "EventShop":
            config = self.alas_config.read_file(self.alas_name)
            with use_scope("group_EventShopPlan", clear=True):
                self._render_event_shop_plan(config)
            return
        if task:
            self.alas_set_group(task)

    @staticmethod
    def _shop_item_identity(item: Mapping[str, Any]) -> tuple[str, str, str, int, int]:
        return (
            str(item.get("id") or ""),
            str(item.get("name") or ""),
            str(item.get("filter") or ""),
            int(item.get("price", 0) or 0),
            int(item.get("stock", 0) or 0),
        )

    @staticmethod
    def _shop_item_dom_key(identity: tuple[str, str, str, int, int]) -> str:
        """Построить стабильный DOM-ключ из исходной идентичности товара."""
        payload = "\x1f".join(
            (identity[0], identity[1], identity[2], str(identity[3]), str(identity[4]))
        )
        return sha256(payload.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def _find_shop_item(cls, items, identity) -> int | None:
        for index, item in enumerate(items):
            if cls._shop_item_identity(item) == identity:
                return index
        return None

    @staticmethod
    def _shop_live_snapshot(plan: Mapping[str, Any], item: Mapping[str, Any]) -> dict[str, int]:
        selected = int(item.get("selected", 0) or 0)
        return {
            "selected": selected,
            "cost": int(item.get("price", 0) or 0) * selected,
            "total": shop_plan_total(plan),
            "selected_count": sum(
                1
                for candidate in plan.get("shop_items", [])
                if int(candidate.get("selected", 0) or 0) > 0
            ),
        }

    def _sync_event_shop_target_state(self, snapshot: Mapping[str, Any]) -> bool:
        event_id = str(snapshot.get("event_id") or "")
        row_id = str(snapshot.get("row_id") or "")
        if not event_id or not row_id:
            logger.warning(
                "[WebUI — магазин события] Цель сохранена без полной идентичности события или товара; синхронизация точки отсчёта пропущена"
            )
            toast(
                "Цель сохранена, но автоматизация магазина не синхронизирована: неполная идентичность события или товара",
                color="warning",
            )
            self._refresh_event_plan_page()
            return False
        try:
            update_event_shop_target_state(
                self.alas_name,
                event_id,
                row_id,
                int(snapshot.get("previous_selected", 0) or 0),
                int(snapshot.get("selected", 0) or 0),
            )
        except (OSError, TypeError, ValueError) as exc:
            logger.warning(
                f"[WebUI — магазин события] Цель сохранена, но не удалось синхронизировать состояние автоматизации: {exc}"
            )
            toast(
                "Цель сохранена, но состояние автоматизации магазина не синхронизировано",
                color="warning",
            )
            self._refresh_event_plan_page()
            return False
        return True

    def _patch_event_shop_plan_values(
        self,
        identity: tuple[str, str, str, int, int],
        snapshot: Mapping[str, int],
    ) -> None:
        """Обновить только изменившиеся числа магазина без пересборки карточек."""
        if getattr(self, "_event_plan_active_task", "") != "EventShop":
            return
        key = self._shop_item_dom_key(identity)
        payload = {
            "selected_id": f"event-shop-selected-{key}",
            "cost_id": f"event-shop-cost-{key}",
            "total_id": "event-shop-plan-total",
            "count_id": "event-shop-plan-count",
            "selected": f"{int(snapshot['selected']):,}".replace(",", " "),
            "cost": f"{int(snapshot['cost']):,}".replace(",", " "),
            "total": f"{int(snapshot['total']):,}".replace(",", " "),
            "count": _plural_positions(int(snapshot["selected_count"])),
        }
        run_js(
            """
((update) => {
  const apply = (id, value) => {
    const node = document.getElementById(id);
    if (!node || node.textContent === value) return;
    node.textContent = value;
    node.classList.remove("event-shop-value-updated");
    requestAnimationFrame(() => {
      node.classList.add("event-shop-value-updated");
      window.setTimeout(
        () => node.classList.remove("event-shop-value-updated"),
        220,
      );
    });
  };
  apply(update.selected_id, update.selected);
  apply(update.cost_id, update.cost);
  apply(update.total_id, update.total);
  apply(update.count_id, update.count);
})(%s);
"""
            % json.dumps(payload, ensure_ascii=False)
        )

    @staticmethod
    def _stale_plan_message() -> None:
        toast(
            "Источник события обновился; страница будет перезагружена.", color="warning"
        )

    def _shop_quantity_popup(self, identity: tuple[str, str, str, int, int]) -> None:
        plan = self._event_plan()
        index = self._find_shop_item(plan["shop_items"], identity)
        if index is None:
            self._stale_plan_message()
            return
        item = plan["shop_items"][index]
        popup(
            f"Количество: {item['name']}",
            [
                put_input(
                    _SHOP_SELECTED_PIN,
                    type="number",
                    label=f"Купить из {item['stock']}",
                    value=item["selected"],
                    min=0,
                    max=item["stock"],
                ),
                put_row(
                    [
                        put_button(
                            "Сохранить",
                            onclick=partial(self._save_shop_quantity_popup, identity),
                            color="primary",
                        ),
                        put_button("Отмена", onclick=close_popup, color="off"),
                    ],
                    size="auto auto",
                ),
            ],
        )

    def _save_shop_quantity_popup(
        self, identity: tuple[str, str, str, int, int]
    ) -> None:
        try:
            selected = int(pin[_SHOP_SELECTED_PIN] or 0)
        except (TypeError, ValueError):
            selected = -1
        if selected < 0 or selected > identity[4]:
            toast(f"Количество должно быть от 0 до {identity[4]}", color="warning")
            return

        live_snapshot: dict[str, int] = {}
        target_snapshot: dict[str, Any] = {}

        def mutation(plan):
            index = self._find_shop_item(plan["shop_items"], identity)
            if index is None:
                return _STALE_EVENT_PLAN
            item = plan["shop_items"][index]
            current = int(item.get("selected", 0) or 0)
            event = plan.get("event", {})
            event_id = str(event.get("id") or "") if isinstance(event, Mapping) else ""
            target_snapshot.update(
                {
                    "event_id": event_id,
                    "row_id": str(item.get("id") or identity[0]),
                    "previous_selected": current,
                    "selected": selected,
                }
            )
            item["selected"] = selected
            live_snapshot.update(self._shop_live_snapshot(plan, item))

        if self._event_plan_mutate(mutation, "Количество в плане обновлено"):
            close_popup()
            if not self._sync_event_shop_target_state(target_snapshot):
                return
            self._patch_event_shop_plan_values(identity, live_snapshot)

    def _change_shop_quantity(
        self,
        identity: tuple[str, str, str, int, int],
        operation: str,
    ) -> None:
        live_snapshot: dict[str, int] = {}
        target_snapshot: dict[str, Any] = {}

        def mutation(plan):
            index = self._find_shop_item(plan["shop_items"], identity)
            if index is None:
                return _STALE_EVENT_PLAN
            item = plan["shop_items"][index]
            current = int(item.get("selected", 0) or 0)
            stock = int(item.get("stock", 0) or 0)
            if operation == "decrement":
                value = current - 1
            elif operation == "increment":
                value = current + 1
            elif operation == "maximum":
                value = stock
            elif operation == "clear":
                value = 0
            else:
                raise ValueError(f"Неизвестная операция количества: {operation}")
            selected = min(max(value, 0), stock)
            if selected == current:
                return _UNCHANGED_EVENT_PLAN
            event = plan.get("event", {})
            event_id = str(event.get("id") or "") if isinstance(event, Mapping) else ""
            target_snapshot.update(
                {
                    "event_id": event_id,
                    "row_id": str(item.get("id") or identity[0]),
                    "previous_selected": current,
                    "selected": selected,
                }
            )
            item["selected"] = selected
            live_snapshot.update(self._shop_live_snapshot(plan, item))

        if self._event_plan_mutate(mutation, ""):
            if not self._sync_event_shop_target_state(target_snapshot):
                return
            self._patch_event_shop_plan_values(identity, live_snapshot)

    def _use_shop_total_as_target(self) -> None:
        total = shop_plan_total(self._event_plan())
        if total <= 0:
            toast("В плане магазина пока нет выбранных товаров", color="warning")
            return
        self._event_config_update({"EventGeneral.EventGeneral.PtLimit": total})
        toast(
            f"Целевой PT установлен по явному действию пользователя: {total}",
            color="success",
        )
        self._refresh_event_plan_page()

    def _apply_shop_plan_to_automation(self) -> None:
        plan = self._event_plan()
        total = shop_plan_total(plan)
        if total <= 0:
            toast("В плане магазина ничего не выбрано", color="warning")
            return
        missing = selected_shop_items_missing_filter(plan)
        if missing:
            toast(
                "Для выбранных товаров нет безопасного токена EventShop: "
                + ", ".join(missing),
                color="warning",
                duration=8,
            )
            return
        tokens = selected_shop_filter_tokens(plan)
        if not tokens:
            toast("Не удалось построить безопасный фильтр магазина", color="warning")
            return
        self._save_config(
            {
                "EventShop.EventShop.PresetFilter": "custom",
                "EventShop.EventShop.CustomFilter": " > ".join(tokens),
                "EventGeneral.EventGeneral.PtLimit": total,
            },
            self.alas_name,
            self.alas_config,
        )
        self.alas_config.load()
        toast("План синхронизирован с EventShop", color="success")
        self._refresh_event_plan_page()
