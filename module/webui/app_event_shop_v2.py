"""Focused EventShop WebUI: purchase priorities and compact task controls."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from functools import lru_cache
from html import escape
from pathlib import Path
from typing import Any

from module.config.deep import deep_get, deep_iter, deep_set
from module.webui.app_dependencies import (
    logger,
    pin_on_change,
    put_html,
    put_input,
    put_output,
    put_row,
    put_scope,
    run_js,
    toast,
    use_scope,
)
from module.webui.app_helpers import is_demo_mode
from module.webui.app_types import WebUIMixinBase
from module.webui.event_assets import event_asset_url
from module.webui.event_shop_priority import (
    load_event_shop_priority,
    set_event_shop_priority,
)


_PRESENTATION_NAMES = (
    Path(__file__).resolve().parent / "data" / "event_shop_names.en.json"
)


@lru_cache(maxsize=1)
def _shop_presentation_names() -> dict[str, str]:
    try:
        raw = json.loads(_PRESENTATION_NAMES.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    names = raw.get("names") if isinstance(raw, Mapping) else None
    if not isinstance(names, Mapping):
        return {}
    return {
        str(source): str(display)
        for source, display in names.items()
        if str(source).strip() and str(display).strip()
    }


class EventShopV2Mixin(WebUIMixinBase):
    """Override only the EventShop surface; other Event pages keep their layout."""

    @staticmethod
    def _event_shop_display_name(name: Any) -> str:
        source = str(name or "")
        return _shop_presentation_names().get(source, source)

    @staticmethod
    def _event_shop_priority_metrics(
        plan: Mapping[str, Any],
        priority_state: Mapping[str, Any],
    ) -> dict[str, int]:
        priorities = dict(priority_state.get("priorities") or {})
        purchased = set(priority_state.get("purchased") or [])
        remembered_remaining = dict(priority_state.get("remaining") or {})
        shop_items = [
            item
            for item in plan.get("shop_items", [])
            if isinstance(item, Mapping)
        ]
        active_rows = [
            item
            for item in shop_items
            if str(item.get("id") or "") in priorities
            and str(item.get("id") or "") not in purchased
        ]
        planned_cost = 0
        for item in active_rows:
            remaining = item.get("remaining")
            row_id = str(item.get("id") or "")
            quantity = (
                max(int(remaining), 0)
                if isinstance(remaining, int)
                else max(int(remembered_remaining[row_id]), 0)
                if row_id in remembered_remaining
                else max(int(item.get("stock", 0) or 0), 0)
            )
            planned_cost += max(int(item.get("price", 0) or 0), 0) * quantity
        return {
            "count": len(active_rows),
            "cost": planned_cost,
        }

    def _patch_event_shop_priority_values(
        self,
        *,
        event_id: str,
        row_id: str,
        live_key: str,
    ) -> None:
        """Patch live priority-derived values without rebuilding the shop grid."""
        plan = self._event_plan()
        event = plan.get("event", {})
        if not isinstance(event, Mapping) or str(event.get("id") or "") != event_id:
            self._refresh_event_plan_page()
            return
        state = load_event_shop_priority(self.alas_name, event_id)
        metrics = self._event_shop_priority_metrics(plan, state)
        priorities = dict(state.get("priorities") or {})
        blocked = dict(state.get("blocked") or {})
        warning = str(blocked.get(row_id) or "") if row_id in priorities else ""
        payload = {
            "count_id": "event-shop-v2-plan-count",
            "cost_id": "event-shop-v2-plan-cost",
            "warning_id": f"event-shop-v2-warning-{live_key}",
            "count": str(metrics["count"]),
            "cost": self._fmt(metrics["cost"]),
            "warning": warning,
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
  apply(update.count_id, update.count);
  apply(update.cost_id, update.cost);
  apply(update.warning_id, update.warning);
})(%s);
"""
            % json.dumps(payload, ensure_ascii=False)
        )

    def _event_shop_priority_changed(
        self,
        event_id: str,
        row_id: str,
        live_key: str,
        raw_value: Any,
    ) -> None:
        if not self._event_write_allowed():
            return
        text = "" if raw_value is None else str(raw_value).strip()
        try:
            priority = None if text == "" else int(text)
        except (TypeError, ValueError, OverflowError):
            toast("Приоритет должен быть целым числом от 0", color="warning")
            self._refresh_event_plan_page()
            return
        if priority is not None and priority < 0:
            toast("Приоритет не может быть отрицательным", color="warning")
            self._refresh_event_plan_page()
            return
        try:
            set_event_shop_priority(
                self.alas_name,
                event_id,
                row_id,
                priority,
            )
        except (OSError, TypeError, ValueError) as exc:
            logger.warning(
                f"[WebUI — магазин события] Не удалось сохранить приоритет: {exc}"
            )
            toast("Не удалось сохранить приоритет покупки", color="error")
            self._refresh_event_plan_page()
            return
        self._patch_event_shop_priority_values(
            event_id=event_id,
            row_id=row_id,
            live_key=live_key,
        )

    def _bind_event_shop_priority(
        self, pin_name: str, event_id: str, row_id: str, live_key: str
    ) -> None:
        bound = getattr(self, "_event_shop_priority_pins", None)
        if bound is None:
            bound = set()
            self._event_shop_priority_pins = bound
        if pin_name in bound:
            return

        def on_change(value: Any) -> None:
            self._event_shop_priority_changed(event_id, row_id, live_key, value)

        pin_on_change(name=pin_name, onchange=on_change)
        bound.add(pin_name)

    def _render_event_shop_task_field(
        self,
        *,
        task: str,
        arg_defs: Mapping[str, Any],
        config: Mapping[str, Any],
        arg_name: str,
        title: str,
    ) -> None:
        definition = arg_defs.get(arg_name)
        if not isinstance(definition, Mapping):
            return
        kwargs = dict(definition)
        display = kwargs.pop("display", None)
        if display == "hide":
            return
        if display == "disabled":
            kwargs["disabled"] = True
        kwargs["widget_type"] = kwargs.pop("type")
        widget_type = str(kwargs["widget_type"])
        kwargs["name"] = f"{task}_Scheduler_{arg_name}"
        kwargs["title"] = title
        value = deep_get(
            config,
            [task, "Scheduler", arg_name],
            kwargs.get("value"),
        )
        kwargs["value"] = str(value) if isinstance(value, datetime) else value
        options = kwargs.pop("option", [])
        kwargs["options"] = options
        kwargs["options_label"] = [str(option) for option in options]
        kwargs["help"] = None
        kwargs["invalid_feedback"] = ""
        output = put_output(kwargs)
        if output is not None:
            output.show()
            if display != "readonly" and widget_type != "stored":
                self._bind_config_watcher([task, "Scheduler", arg_name])

    def _render_event_shop_task_settings(
        self,
        *,
        task: str,
        group_map: Mapping[str, Any],
        config: dict[str, Any],
    ) -> None:
        scheduler = group_map.get("Scheduler")
        if scheduler is None:
            return
        _, arg_defs = scheduler
        if not isinstance(arg_defs, Mapping):
            return

        if not bool(deep_get(config, [task, "Scheduler", "Sensitive"], False)):
            if not is_demo_mode():
                try:
                    self._event_config_update(
                        {f"{task}.Scheduler.Sensitive": True}
                    )
                    deep_set(config, [task, "Scheduler", "Sensitive"], True)
                except Exception as exc:
                    logger.warning(
                        f"[WebUI — магазин события] Не удалось закрепить чувствительный режим задачи: {exc}"
                    )

        put_html(
            '<div class="event-shop-task-heading">'
            '<strong>Настройки задачи</strong>'
            '<small>Только параметры запуска магазина.</small>'
            "</div>"
        )
        put_scope("event_shop_task_fields")
        with use_scope("event_shop_task_fields"):
            self._render_event_shop_task_field(
                task=task,
                arg_defs=arg_defs,
                config=config,
                arg_name="Enable",
                title="Включить эту функцию",
            )
            self._render_event_shop_task_field(
                task=task,
                arg_defs=arg_defs,
                config=config,
                arg_name="PushNotification",
                title="Push-уведомление об ошибке",
            )
            self._render_event_shop_task_field(
                task=task,
                arg_defs=arg_defs,
                config=config,
                arg_name="NextRun",
                title="Время следующего запуска",
            )

    def _render_event_shop_priority_plan(self, config: Mapping[str, Any]) -> None:
        """Render the clean priority UI without legacy automation diagnostics."""
        plan = self._event_plan()
        event = plan.get("event", {})
        if not isinstance(event, Mapping):
            event = {}
        event_id = str(event.get("id") or "")
        priority_state = load_event_shop_priority(self.alas_name, event_id)
        priorities = dict(priority_state.get("priorities") or {})
        purchased = set(priority_state.get("purchased") or [])
        remembered_remaining = dict(priority_state.get("remaining") or {})
        blocked = dict(priority_state.get("blocked") or {})
        shop_items = [
            item
            for item in plan.get("shop_items", [])
            if isinstance(item, Mapping)
        ]
        metrics = self._event_shop_priority_metrics(plan, priority_state)
        planned_cost = metrics["cost"]

        progress = plan.get("progress", {})
        current_pt = (
            progress.get("current_pt")
            if isinstance(progress, Mapping)
            and isinstance(progress.get("current_pt"), int)
            else None
        )
        currencies = [
            item
            for item in plan.get("currencies", [])
            if isinstance(item, Mapping)
        ]
        currency = currencies[0] if currencies else {}
        currency_asset = event_asset_url(currency.get("asset"))
        shop_end = escape(str(event.get("shop_end") or "Не задано"))

        put_html(
            f"""
<section class="event-shop-v2-hero">
  <div>
    <div class="event-eyebrow">Магазин текущего ивента</div>
    <h3>{escape(str(event.get("name") or "Текущий ивент"))}</h3>
    <small>Доступен до {shop_end}</small>
  </div>
  <div class="event-shop-v2-metrics">
    <span><b>{self._fmt(current_pt) if current_pt is not None else "—"}</b><small>Баланс</small></span>
    <span><b id="event-shop-v2-plan-count">{metrics["count"]}</b><small>В плане</small></span>
    <span><b id="event-shop-v2-plan-cost">{self._fmt(planned_cost)}</b><small>Стоимость плана</small></span>
  </div>
</section>
<div class="event-shop-priority-help">
  Пустое поле — игнорировать товар. 0 — самый высокий приоритет; чем больше число, тем ниже приоритет.
</div>
"""
        )

        put_scope("event_shop_v2_grid")
        with use_scope("event_shop_v2_grid"):
            for item in shop_items:
                row_id = str(item.get("id") or "")
                identity = self._shop_item_identity(dict(item))
                live_key = self._shop_item_dom_key(identity)
                pin_name = f"event_shop_priority_{live_key}"
                is_purchased = row_id in purchased
                remaining = item.get("remaining")
                if is_purchased:
                    available = 0
                elif isinstance(remaining, int):
                    available = max(remaining, 0)
                elif row_id in remembered_remaining:
                    available = max(int(remembered_remaining[row_id]), 0)
                else:
                    available = max(int(item.get("stock", 0) or 0), 0)
                rarity = item.get("rarity")
                rarity_html = (
                    f'<span class="event-shop-v2-rarity">Редкость {escape(str(rarity))}</span>'
                    if rarity is not None
                    else ""
                )
                state_html = (
                    '<span class="event-shop-v2-bought">Куплено</span>'
                    if is_purchased
                    else f'<span class="event-shop-v2-stock">Доступно: {available}</span>'
                )
                asset_url = event_asset_url(item.get("asset"))
                price = max(int(item.get("price", 0) or 0), 0)
                warning = blocked.get(row_id, "") if row_id in priorities else ""
                card_class = " event-shop-v2-card-bought" if is_purchased else ""
                put_scope(
                    f"event_shop_card_{live_key}",
                    [
                        put_html(
                            f"""
<article class="event-shop-v2-card{card_class}">
  <div class="event-shop-v2-card-top">{state_html}{rarity_html}</div>
  <img class="event-shop-v2-image" src="{escape(asset_url)}" alt="">
  <strong class="event-shop-v2-name">{escape(self._event_shop_display_name(item.get("name")))}</strong>
  <div class="event-shop-v2-price"><img src="{escape(currency_asset)}" alt=""><b>{self._fmt(price)}</b></div>
  <small id="event-shop-v2-warning-{live_key}" class="event-shop-v2-warning">{escape(str(warning or ""))}</small>
</article>
"""
                        ),
                        put_input(
                            pin_name,
                            type="number",
                            label="Приоритет покупки",
                            value=""
                            if row_id not in priorities
                            else int(priorities[row_id]),
                            placeholder="Игнорировать",
                            readonly=is_purchased,
                            min=0,
                            step=1,
                        ).style("--event-shop-v2-priority--"),
                    ],
                )
                self._bind_event_shop_priority(
                    pin_name,
                    event_id,
                    row_id,
                    live_key,
                )

    def _render_event_shop_layout(self, *, task, group_map, config) -> None:
        with use_scope("groups"):
            put_row(
                [
                    put_scope("group_EventShopPlan"),
                    put_scope("group_EventShopTaskSettings"),
                ],
                size="minmax(0, 1fr) 300px",
            ).style("--event-shop-v2-layout--")
        with use_scope("group_EventShopTaskSettings", clear=True):
            self._render_event_shop_task_settings(
                task=task,
                group_map=group_map,
                config=config,
            )
        with use_scope("group_EventShopPlan", clear=True):
            self._render_event_shop_priority_plan(config)
