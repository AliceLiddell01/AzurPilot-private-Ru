"""Компактный EventShop WebUI: цели количества, приоритеты покупок и настройки задачи."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from functools import lru_cache, partial
from html import escape
from pathlib import Path
from typing import Any

from module.config.deep import deep_get
from module.webui.app_dependencies import (
    logger,
    pin_on_change,
    put_button,
    put_html,
    put_input,
    put_output,
    put_row,
    put_scope,
    run_js,
    toast,
    use_scope,
)
from module.webui.app_types import WebUIMixinBase
from module.webui.event_assets import event_asset_url
from module.webui.event_shop_priority import (
    event_shop_target_capacity,
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
    """Переопределить только поверхность EventShop, не меняя остальные Event-страницы."""

    @staticmethod
    def _event_shop_display_name(name: Any) -> str:
        source = str(name or "")
        return _shop_presentation_names().get(source, source)

    @staticmethod
    def _event_shop_target_remaining(
        item: Mapping[str, Any],
        priority_state: Mapping[str, Any],
    ) -> int:
        """Вернуть ещё не выполненную часть текущей пользовательской цели количества."""
        row_id = str(item.get("id") or "")
        stock = max(int(item.get("stock", 0) or 0), 0)
        priorities = set(priority_state.get("priorities") or {})
        purchased = set(priority_state.get("purchased") or [])
        remembered_remaining = dict(priority_state.get("remaining") or {})
        target_baselines = dict(priority_state.get("target_baselines") or {})

        if row_id in purchased:
            available = 0
        elif isinstance(item.get("remaining"), int):
            available = min(max(int(item["remaining"]), 0), stock)
        elif row_id in remembered_remaining:
            available = min(max(int(remembered_remaining[row_id]), 0), stock)
        else:
            available = stock

        selected = min(max(int(item.get("selected", 0) or 0), 0), stock)
        if row_id not in priorities and row_id not in purchased:
            return selected

        if row_id in target_baselines:
            baseline = min(max(int(target_baselines[row_id]), 0), stock)
            baseline = max(baseline, available)
        else:
            baseline = stock
        selected = min(selected, baseline)
        bought_for_goal = max(baseline - available, 0)
        return max(selected - bought_for_goal, 0)

    @staticmethod
    def _event_shop_target_bought(
        item: Mapping[str, Any],
        priority_state: Mapping[str, Any],
    ) -> int:
        """Вернуть доказанное число покупок в текущем эпизоде цели."""
        row_id = str(item.get("id") or "")
        stock = max(int(item.get("stock", 0) or 0), 0)
        purchased = set(priority_state.get("purchased") or [])
        remembered_remaining = dict(priority_state.get("remaining") or {})
        target_baselines = dict(priority_state.get("target_baselines") or {})

        if row_id in purchased:
            available = 0
        elif isinstance(item.get("remaining"), int):
            available = min(max(int(item["remaining"]), 0), stock)
        elif row_id in remembered_remaining:
            available = min(max(int(remembered_remaining[row_id]), 0), stock)
        else:
            available = stock

        if row_id in target_baselines:
            baseline = min(max(int(target_baselines[row_id]), 0), stock)
            baseline = max(baseline, available)
            return max(baseline - available, 0)

        selected = min(max(int(item.get("selected", 0) or 0), 0), stock)
        remaining = EventShopV2Mixin._event_shop_target_remaining(
            item, priority_state
        )
        return max(selected - remaining, 0)

    @classmethod
    def _event_shop_priority_metrics(
        cls,
        plan: Mapping[str, Any],
        priority_state: Mapping[str, Any],
    ) -> dict[str, int]:
        priorities = dict(priority_state.get("priorities") or {})
        purchased = set(priority_state.get("purchased") or [])
        active_count = 0
        planned_cost = 0
        for item in plan.get("shop_items", []):
            if not isinstance(item, Mapping):
                continue
            row_id = str(item.get("id") or "")
            if row_id not in priorities or row_id in purchased:
                continue
            remaining_target = cls._event_shop_target_remaining(item, priority_state)
            if remaining_target <= 0:
                continue
            active_count += 1
            planned_cost += (
                max(int(item.get("price", 0) or 0), 0) * remaining_target
            )
        return {"count": active_count, "cost": planned_cost}

    @staticmethod
    def _run_event_shop_dom_patch(payload: Mapping[str, Any]) -> None:
        run_js(
            """
((update) => {
  const apply = (id, value) => {
    if (!id) return;
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
  for (const item of update.values || []) apply(item.id, item.value);
})(%s);
"""
            % json.dumps(dict(payload), ensure_ascii=False)
        )

    def _patch_event_shop_priority_values(
        self,
        *,
        event_id: str,
        row_id: str,
        live_key: str,
    ) -> None:
        """Обновить зависимые от приоритета значения без перестройки каталога."""
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
        shop_item = next(
            (
                item
                for item in plan.get("shop_items", [])
                if isinstance(item, Mapping) and str(item.get("id") or "") == row_id
            ),
            None,
        )
        if not isinstance(shop_item, Mapping):
            self._refresh_event_plan_page()
            return
        remaining_target = self._event_shop_target_remaining(shop_item, state)
        self._run_event_shop_dom_patch(
            {
                "values": [
                    {
                        "id": f"event-shop-target-left-{live_key}",
                        "value": self._fmt(remaining_target),
                    },
                    {
                        "id": "event-shop-v2-plan-count",
                        "value": str(metrics["count"]),
                    },
                    {
                        "id": "event-shop-v2-plan-cost",
                        "value": self._fmt(metrics["cost"]),
                    },
                    {
                        "id": f"event-shop-v2-warning-{live_key}",
                        "value": warning,
                    },
                ]
            }
        )

    def _patch_event_shop_plan_values(
        self,
        identity: tuple[str, str, str, int, int],
        snapshot: Mapping[str, int],
    ) -> None:
        """Обновить цель количества и метрики приоритета после +/-/MAX/сброса."""
        plan = self._event_plan()
        event = plan.get("event", {})
        if not isinstance(event, Mapping):
            self._refresh_event_plan_page()
            return
        event_id = str(event.get("id") or "")
        index = self._find_shop_item(plan.get("shop_items", []), identity)
        if index is None:
            self._refresh_event_plan_page()
            return
        item = plan["shop_items"][index]
        state = load_event_shop_priority(self.alas_name, event_id)
        metrics = self._event_shop_priority_metrics(plan, state)
        live_key = self._shop_item_dom_key(identity)
        capacity = event_shop_target_capacity(item, state)
        remaining_target = self._event_shop_target_remaining(item, state)
        selected = max(int(snapshot["selected"]), 0)
        active_target_episode = (
            str(item.get("id") or "")
            in dict(state.get("target_baselines") or {})
            and selected > 0
        )
        target_label = (
            "Цель эпизода" if active_target_episode else "Цель покупки"
        )
        bought_for_target = self._event_shop_target_bought(item, state)
        self._run_event_shop_dom_patch(
            {
                "values": [
                    {
                        "id": f"event-shop-selected-{live_key}",
                        "value": self._fmt(selected),
                    },
                    {
                        "id": f"event-shop-cost-{live_key}",
                        "value": self._fmt(snapshot["cost"]),
                    },
                    {
                        "id": f"event-shop-capacity-{live_key}",
                        "value": self._fmt(capacity) if capacity is not None else "—",
                    },
                    {
                        "id": f"event-shop-target-label-{live_key}",
                        "value": target_label,
                    },
                    {
                        "id": f"event-shop-target-bought-{live_key}",
                        "value": self._fmt(bought_for_target),
                    },
                    {
                        "id": f"event-shop-target-left-{live_key}",
                        "value": self._fmt(remaining_target),
                    },
                    {
                        "id": "event-shop-v2-plan-count",
                        "value": str(metrics["count"]),
                    },
                    {
                        "id": "event-shop-v2-plan-cost",
                        "value": self._fmt(metrics["cost"]),
                    },
                ]
            }
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

        put_html(
            '<div class="event-shop-task-heading">'
            "<strong>Настройки задачи</strong>"
            "<small>Запуск магазина и уведомление о штатном завершении.</small>"
            "</div>"
        )
        put_scope("event_shop_task_fields")
        with use_scope("event_shop_task_fields"):
            self._render_event_shop_task_field(
                task=task,
                arg_defs=arg_defs,
                config=config,
                arg_name="Enable",
                title="Включить функцию",
            )
            self._render_event_shop_task_field(
                task=task,
                arg_defs=arg_defs,
                config=config,
                arg_name="PushNotification",
                title="Уведомлять о завершении",
            )
            self._render_event_shop_task_field(
                task=task,
                arg_defs=arg_defs,
                config=config,
                arg_name="NextRun",
                title="Следующий запуск",
            )
        put_html(
            '<small class="event-shop-task-note">'
            'Ошибки используют общий канал OnePush из настроек ошибок. '
            'Если провайдер там не задан, внешний push об ошибке отправить невозможно.'
            '</small>'
        )

    def _render_event_shop_priority_plan(self, config: Mapping[str, Any]) -> None:
        """Отрисовать цели количества и независимый порядок покупок."""
        del config
        plan = self._event_plan()
        event = plan.get("event", {})
        if not isinstance(event, Mapping):
            event = {}
        event_id = str(event.get("id") or "")
        priority_state = load_event_shop_priority(self.alas_name, event_id)
        priorities = dict(priority_state.get("priorities") or {})
        purchased = set(priority_state.get("purchased") or [])
        completed = set(priority_state.get("completed") or [])
        remembered_remaining = dict(priority_state.get("remaining") or {})
        target_baselines = dict(priority_state.get("target_baselines") or {})
        blocked = dict(priority_state.get("blocked") or {})
        shop_items = [
            item
            for item in plan.get("shop_items", [])
            if isinstance(item, Mapping)
        ]
        metrics = self._event_shop_priority_metrics(plan, priority_state)

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
        currency_name = escape(str(currency.get("name") or "Валюта события"))
        shop_end = escape(str(event.get("shop_end") or "Не задано"))

        put_html(
            f"""
<section class="event-shop-v2-hero">
  <div class="event-shop-v2-title">
    <div class="event-eyebrow">Магазин текущего ивента</div>
    <h3>{escape(str(event.get("name") or "Текущий ивент"))}</h3>
    <small>Доступен до {shop_end}</small>
  </div>
  <div class="event-shop-v2-metrics">
    <span class="event-shop-v2-balance"><img src="{escape(currency_asset)}" alt="{currency_name}"><b>{self._fmt(current_pt) if current_pt is not None else "—"}</b><small>Баланс</small></span>
    <span><b id="event-shop-v2-plan-count">{metrics["count"]}</b><small>Активных целей</small></span>
    <span><b id="event-shop-v2-plan-cost">{self._fmt(metrics["cost"])}</b><small>Осталось по плану</small></span>
  </div>
</section>
<div class="event-shop-priority-help">
  <strong>Цель</strong> задаёт размер эпизода покупки. Для активной цели исходный предел сохраняется, а поле «Осталось купить» показывает её фактически невыполненную часть. <strong>Приоритет</strong> задаёт порядок: 0 выше 1, 1 выше 2. Для автоматической покупки должны быть заданы и цель больше 0, и приоритет.
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

                stock = max(int(item.get("stock", 0) or 0), 0)
                remaining = item.get("remaining")
                if is_purchased:
                    available = 0
                elif isinstance(remaining, int):
                    available = min(max(remaining, 0), stock)
                elif row_id in remembered_remaining:
                    available = min(
                        max(int(remembered_remaining[row_id]), 0), stock
                    )
                else:
                    available = stock

                selected = min(max(int(item.get("selected", 0) or 0), 0), stock)
                capacity = event_shop_target_capacity(item, priority_state)
                target_remaining = self._event_shop_target_remaining(
                    item, priority_state
                )
                active_target_episode = row_id in target_baselines and selected > 0
                target_label = (
                    "Цель эпизода" if active_target_episode else "Цель покупки"
                )
                bought_for_target = self._event_shop_target_bought(
                    item, priority_state
                )
                target_done = (
                    not is_purchased
                    and (
                        (row_id in completed and selected == 0)
                        or (
                            row_id in priorities
                            and selected > 0
                            and target_remaining == 0
                        )
                    )
                )

                rarity = item.get("rarity")
                rarity_html = (
                    f'<span class="event-shop-v2-rarity">Редкость {escape(str(rarity))}</span>'
                    if rarity is not None
                    else ""
                )
                if is_purchased:
                    status_html = '<span class="event-shop-v2-bought">Полностью куплено</span>'
                elif target_done:
                    status_html = '<span class="event-shop-v2-done">Цель выполнена</span>'
                else:
                    status_html = ""
                availability_html = (
                    f'<span class="event-shop-v2-stock">Доступно: {available}</span>'
                )
                state_html = status_html + availability_html

                asset_url = event_asset_url(item.get("asset"))
                price = max(int(item.get("price", 0) or 0), 0)
                warning = (
                    blocked.get(row_id, "") if row_id in priorities else ""
                )
                card_class = (
                    " event-shop-v2-card-bought"
                    if is_purchased
                    else " event-shop-v2-card-done"
                    if target_done
                    else ""
                )

                put_scope(
                    f"event_shop_card_{live_key}",
                    [
                        put_html(
                            f"""
<article class="event-shop-v2-card{card_class}">
  <div class="event-shop-v2-card-top">{state_html}{rarity_html}</div>
  <img class="event-shop-v2-image" src="{escape(asset_url)}" alt="">
  <strong class="event-shop-v2-name">{escape(self._event_shop_display_name(item.get("name")))}</strong>
  <div class="event-shop-v2-price"><img src="{escape(currency_asset)}" alt="{currency_name}"><b>{self._fmt(price)}</b></div>
  <div class="event-shop-v2-target">
    <span id="event-shop-target-label-{live_key}">{target_label}</span>
    <strong><span id="event-shop-selected-{live_key}" class="event-shop-live-value">{self._fmt(selected)}</span></strong>
    <small>Допустимый максимум: <span id="event-shop-capacity-{live_key}" class="event-shop-live-value">{self._fmt(capacity) if capacity is not None else "—"}</span></small>
    <small>Уже куплено по цели: <span id="event-shop-target-bought-{live_key}" class="event-shop-live-value">{self._fmt(bought_for_target)}</span></small>
    <small>Осталось купить: <span id="event-shop-target-left-{live_key}" class="event-shop-live-value">{self._fmt(target_remaining)}</span></small>
    <small>Стоимость цели: <span id="event-shop-cost-{live_key}" class="event-shop-live-value">{self._fmt(price * selected)}</span></small>
  </div>
  <small id="event-shop-v2-warning-{live_key}" class="event-shop-v2-warning">{escape(str(warning or ""))}</small>
</article>
"""
                        ),
                        put_row(
                            [
                                put_button(
                                    "−",
                                    onclick=partial(
                                        self._change_shop_quantity,
                                        identity,
                                        "decrement",
                                    ),
                                    color="off",
                                ),
                                put_button(
                                    "+",
                                    onclick=partial(
                                        self._change_shop_quantity,
                                        identity,
                                        "increment",
                                    ),
                                    color="off",
                                ),
                                put_button(
                                    "MAX",
                                    onclick=partial(
                                        self._change_shop_quantity,
                                        identity,
                                        "maximum",
                                    ),
                                    color="off",
                                ),
                                put_button(
                                    "Сброс",
                                    onclick=partial(
                                        self._change_shop_quantity,
                                        identity,
                                        "clear",
                                    ),
                                    color="off",
                                ),
                            ],
                            size="auto auto auto auto",
                        ).style("--event-shop-v2-target-controls--"),
                        put_input(
                            pin_name,
                            type="number",
                            label="Приоритет",
                            value=""
                            if row_id not in priorities
                            else int(priorities[row_id]),
                            placeholder="Не задан",
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

    def _render_event_shop_plan(self, config: Mapping[str, Any]) -> None:
        """Сохранять частичные обновления на V2-поверхности вместо legacy-сетки."""
        self._render_event_shop_priority_plan(config)

    def _render_event_shop_layout(self, *, task, group_map, config) -> None:
        with use_scope("groups"):
            put_row(
                [
                    put_scope("group_EventShopPlan"),
                    put_scope("group_EventShopTaskSettings"),
                ],
                size="minmax(0, 1fr) minmax(330px, 360px)",
            ).style("--event-shop-v2-layout--")
        with use_scope("group_EventShopTaskSettings", clear=True):
            self._render_event_shop_task_settings(
                task=task,
                group_map=group_map,
                config=config,
            )
        with use_scope("group_EventShopPlan", clear=True):
            self._render_event_shop_priority_plan(config)
