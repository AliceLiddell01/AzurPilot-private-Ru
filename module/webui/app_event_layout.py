"""Основной presentation-layer страниц Event без дублирующих legacy-render путей."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable, Mapping
from functools import partial
from html import escape
from typing import Any

from module.webui.app_dependencies import (
    close_popup,
    current_time,
    deep_get,
    deep_iter,
    logger,
    pin,
    popup,
    put_button,
    put_collapse,
    put_html,
    put_input,
    put_none,
    put_row,
    put_scope,
    run_js,
    t,
    toast,
    to_server,
    use_scope,
)
from module.webui.app_event_planner import EventPlannerMixin
from module.webui.app_helpers import is_demo_mode
from module.webui.event_assets import event_asset_url
from module.webui.event_plan import shop_plan_total
from module.webui.event_source import resolve_current_event_artifact

EVENT_MAP_TASKS = frozenset({"Event", "Event2", "Event3"})
EVENT_LAYOUT_TASKS = EVENT_MAP_TASKS | {"EventShop"}
EVENT_MODERN_TASKS = EVENT_LAYOUT_TASKS | {"EventGeneral", "EventRewards"}
EVENT_MAP_PRIMARY_GROUPS = (
    "Scheduler",
    "Campaign",
    "StopCondition",
    "Fleet",
    "Emotion",
)
EVENT_MAP_ADVANCED_GROUPS = ("Submarine", "HpControl", "EnemyPriority")

_TARGET_PT = "event_modern_target_pt"


class EventLayoutMixin(EventPlannerMixin):
    """Рендерить Event-карты и магазин через один канонический layout-контракт."""

    @staticmethod
    def _event_group_map(task_args: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
        return {
            group[0]: (group, args) for group, args in deep_iter(task_args, depth=1)
        }

    @staticmethod
    def _fmt(value: Any) -> str:
        try:
            return f"{int(value):,}".replace(",", " ")
        except (TypeError, ValueError):
            return str(value)

    @staticmethod
    def _event_write_allowed() -> bool:
        if not is_demo_mode():
            return True
        toast(
            "В демонстрационном режиме изменение настроек ивента отключено.",
            color="warning",
        )
        return False

    def _mark_event_page(self, task: str) -> None:
        run_js(
            """
(() => {
  const content = document.getElementById("pywebio-scope-content");
  if (content) {
    content.classList.add("event-modern-page");
    content.dataset.eventTask = %s;
  }
  document.body.classList.add("event-modern-active");
  document.body.dataset.eventTask = %s;
})();
"""
            % (json.dumps(task), json.dumps(task))
        )

    @staticmethod
    def _unmark_event_page() -> None:
        run_js(
            """
(() => {
  const content = document.getElementById("pywebio-scope-content");
  if (content) {
    content.classList.remove("event-modern-page");
    delete content.dataset.eventTask;
  }
  document.body.classList.remove("event-modern-active");
  delete document.body.dataset.eventTask;
})();
"""
        )

    def init_menu(self, collapse_menu: bool = True, name: str | None = None) -> None:
        """Снять Event-only DOM state при переходе на другую поверхность WebUI."""
        if name not in EVENT_MODERN_TASKS:
            self._unmark_event_page()
        super().init_menu(collapse_menu=collapse_menu, name=name)

    def _render_named_group(self, task, name, group_map, config, navigator=True) -> int:
        item = group_map.get(name)
        if item is None:
            return 0
        group, args = item
        rendered = self.set_group(group, args, config, task)
        if rendered and navigator:
            self.set_navigator(group)
        return rendered

    def _render_advanced(
        self,
        *,
        task: str,
        title: str,
        description: str,
        names: Iterable[str],
        group_map,
        config,
    ) -> None:
        """Сразу рендерить редкие группы внутрь collapse без последующего DOM reparent."""
        existing = [name for name in names if name in group_map]
        if not existing:
            return
        key = "-".join(name.lower() for name in existing)
        body_scope = f"event_advanced_{task.lower()}_{key}"
        with use_scope("groups"):
            put_collapse(
                title,
                [
                    put_html(
                        f'<div class="event-advanced-description">{escape(description)}</div>'
                    ),
                    put_scope(body_scope),
                ],
                open=False,
            ).style("--event-advanced-details--")
        with use_scope(body_scope, clear=True):
            for name in existing:
                self._render_named_group(task, name, group_map, config, False)

    def _settings_popup(self) -> None:
        config = self.alas_config.read_file(self.alas_name)
        target = int(deep_get(config, "EventGeneral.EventGeneral.PtLimit", 0) or 0)
        popup(
            "Настроить цель фарма",
            [
                put_html('<span class="event-modern-dialog-marker"></span>'),
                put_input(
                    _TARGET_PT,
                    type="number",
                    min=0,
                    label="Автостоп по PT",
                    value=target,
                    help_text="0 — без ограничения. План магазина учитывается в прогнозе, но не меняет balance-based PT-автостоп.",
                ),
                put_row(
                    [
                        put_button(
                            "Сохранить",
                            onclick=self._save_settings_popup,
                            color="primary",
                        ),
                        put_button("Отмена", onclick=close_popup, color="off"),
                    ],
                    size="auto auto",
                ),
            ],
        )

    def _save_settings_popup(self) -> None:
        if not self._event_write_allowed():
            return
        try:
            target_pt = int(pin[_TARGET_PT] or 0)
        except (TypeError, ValueError):
            target_pt = -1
        if target_pt < 0:
            toast("PT не может быть отрицательным", color="warning")
            return
        try:
            self._event_config_update({"EventGeneral.EventGeneral.PtLimit": target_pt})
        except Exception as exc:
            logger.exception(exc)
            toast(f"Не удалось сохранить цель фарма: {exc}", color="error")
            return

        close_popup()
        toast("Цель PT сохранена", color="success")
        self._refresh_event_plan_page()

    def _render_event_shop_plan(self, config: Mapping[str, Any]) -> None:
        self._event_plan_active_task = "EventShop"
        plan = self._event_plan()
        items = list(plan["shop_items"])
        total = shop_plan_total(plan)
        selected = [item for item in items if int(item.get("selected", 0) or 0) > 0]
        catalog_total = sum(
            int(item.get("price", 0) or 0) * int(item.get("stock", 0) or 0)
            for item in items
        )
        observed_known = bool(items) and all(
            item.get("match_status") == "matched"
            and isinstance(item.get("remaining"), int)
            for item in items
        )
        observed_total = (
            sum(
                int(item.get("price", 0) or 0) * int(item["remaining"])
                for item in items
            )
            if observed_known
            else None
        )
        currencies = {
            int(item.get("id", 0) or 0): item
            for item in plan.get("currencies", [])
            if isinstance(item, Mapping)
        }
        primary_currency = next(iter(currencies.values()), {})
        currency_icon = event_asset_url(
            primary_currency.get("asset")
            if isinstance(primary_currency, Mapping)
            else None
        )
        shop_end = str(plan["event"].get("shop_end") or "Не задано")
        put_html(f"""
<section class="event-shop-hero"><div><div class="event-eyebrow">Каталог текущего EN-ивента</div>
<h3>{escape(str(plan["event"].get("name") or "Текущий ивент не задан"))}</h3>
<p>Магазин доступен до {escape(shop_end)}. Желаемое количество не подменяет runtime-наблюдение.</p>
<div class="event-shop-currency"><img src="{escape(currency_icon)}" alt=""><span>{escape(str(primary_currency.get("name") or "Валюта события"))}</span></div></div>
<div class="event-shop-totals">
  <div><span>Полный выкуп</span><strong>{self._fmt(catalog_total)}</strong><small>{len(items)} товаров</small></div>
  <div><span>Ваш план</span><strong id="event-shop-plan-total" class="event-shop-live-value">{self._fmt(total)}</strong><small id="event-shop-plan-count" class="event-shop-live-value">{len(selected)} позиций</small></div>
  <div><span>Осталось по scan</span><strong>{self._fmt(observed_total) if observed_total is not None else "Нет данных"}</strong><small>{"Полный snapshot" if observed_known else "Наблюдение недоступно"}</small></div>
</div></section>""")
        put_scope("event_shop_safety_status")
        if items:
            put_scope("event_shop_grid")
            card_scope_ids = []
            with use_scope("event_shop_grid"):
                for index, item in enumerate(items):
                    identity = self._shop_item_identity(item)
                    live_key = self._shop_item_dom_key(identity)
                    observation_label = {
                        "matched": "Наблюдение сопоставлено",
                        "ambiguous": "Наблюдение неоднозначно",
                        "unmatched": "Не сопоставлено",
                        "invalid_counter": "Ошибка счётчика",
                        "unavailable": "Нет наблюдения",
                    }.get(
                        str(item.get("match_status") or "unavailable"),
                        "Нет наблюдения",
                    )
                    scope_id = f"event_shop_card_{index}"
                    card_scope_ids.append(f"pywebio-scope-{scope_id}")
                    put_scope(scope_id)
                    currency = currencies.get(int(item.get("currency_id", 0) or 0), {})
                    with use_scope(scope_id):
                        put_html(
                            '<div class="event-shop-card-visual">'
                            f'<span class="event-shop-stock">Доступно: {escape(self._fmt(item.get("stock")))}</span>'
                            f'<img src="{escape(event_asset_url(item.get("asset")))}" alt="{escape(str(item.get("name") or "Товар"))}">'
                            f'<span class="event-shop-rarity event-rarity-{escape(str(item.get("rarity") or "unknown"))}">Редкость {escape(str(item.get("rarity") if item.get("rarity") is not None else "—"))}</span>'
                            f'<h4>{escape(str(item.get("name") or "Без названия"))}</h4>'
                            f'<small>{escape(str(item.get("category") or "неизвестно"))} · набор ×{escape(self._fmt(item.get("amount", 1)))}</small>'
                            '<div class="event-shop-price">'
                            f'<img src="{escape(event_asset_url(currency.get("asset") if isinstance(currency, Mapping) else None))}" alt="">'
                            f'<strong>{escape(self._fmt(item.get("price")))}</strong></div></div>'
                            '<div class="event-shop-observation">'
                            f'<span>{escape(observation_label)}</span>'
                            f'<small>Куплено: {escape(self._fmt(item.get("purchased")) if item.get("purchased") is not None else "Нет данных")} · Осталось: {escape(self._fmt(item.get("remaining")) if item.get("remaining") is not None else "Нет данных")}</small></div>'
                            '<div class="event-shop-desired">'
                            f'<span>Цель</span><strong><span id="event-shop-selected-{live_key}" class="event-shop-live-value">{escape(self._fmt(item.get("selected")))}</span> / {escape(self._fmt(item.get("stock")))}</strong>'
                            f'<small>Стоимость: <span id="event-shop-cost-{live_key}" class="event-shop-live-value">{escape(self._fmt(int(item.get("price", 0) or 0) * int(item.get("selected", 0) or 0)))}</span></small></div>'
                            f'<div class="event-shop-automation">{"Совместимо с автоматизацией" if item.get("filter") else "Автоматизация не поддерживается"}</div>'
                        )
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
                        )
            run_js(
                """
(() => {
  const grid = document.getElementById("pywebio-scope-event_shop_grid");
  if (grid) grid.classList.add("event-shop-grid");
  for (const id of %s) {
    const card = document.getElementById(id);
    if (card) card.classList.add("event-shop-card");
  }
})();
"""
                % json.dumps(card_scope_ids)
            )
        else:
            put_html(
                '<div class="event-empty-card"><strong>Каталог магазина отсутствует в datamine artifact</strong></div>'
            )

    @staticmethod
    def _event_server(config: Mapping[str, Any]) -> str | None:
        """Безопасно определить сервер текущей конфигурации по PackageName."""
        package_name = str(
            deep_get(config, ["Alas", "Emulator", "PackageName"], "") or ""
        ).strip()
        if not package_name:
            return None
        try:
            server = str(to_server(package_name) or "").strip().lower()
        except ValueError:
            return None
        return server or None

    def _current_event_name(self, config: Mapping[str, Any]) -> str | None:
        """Получить отображаемое имя текущего события из активного Event artifact."""
        if is_demo_mode():
            return None
        server = self._event_server(config)
        if server is None:
            return None
        artifact, unavailable = resolve_current_event_artifact(
            server=server.upper(), now=current_time()
        )
        if artifact is None:
            if unavailable:
                logger.warning("[WebUI — ивент] Текущий Event artifact недоступен")
            return None
        spec = artifact.get("event_spec")
        if not isinstance(spec, Mapping):
            return None
        name = str(spec.get("name") or "").strip()
        return name or None

    def _prepare_event_map_args(
        self,
        task: str,
        config: Mapping[str, Any],
    ) -> tuple[dict[str, Any], Mapping[str, Any], str | None]:
        """Скрыть selector локально, если его значение есть среди доступных вариантов.

        Config и глобальный i18n не изменяются.
        """
        task_args = copy.deepcopy(dict(self.ALAS_ARGS[task]))
        server = self._event_server(config)
        if server is None:
            return task_args, config, None
        event_name = self._current_event_name(config)
        if event_name is None:
            return task_args, config, None

        selector = str(
            deep_get(config, [task, "Campaign", "Event"], "") or ""
        ).strip()
        if not selector.startswith("event_"):
            return task_args, config, None
        campaign = task_args.get("Campaign")
        if not isinstance(campaign, dict):
            return task_args, config, None
        event_arg = campaign.get("Event")
        if not isinstance(event_arg, dict):
            return task_args, config, None
        options = {
            str(item)
            for field in ("option", f"option_{server}")
            for item in (event_arg.get(field) or [])
        }
        if selector not in options:
            return task_args, config, None

        event_arg["display"] = "hide"
        return task_args, config, event_name

    def _render_event_map_layout(
        self,
        *,
        task: str,
        group_map: Mapping[str, Any],
        config: Mapping[str, Any],
        current_event_name: str | None,
    ) -> None:
        with use_scope("groups"):
            put_html(
                '<div class="event-map-intro"><span>Ивентовая карта</span>'
                '<small>Основное — на виду, редкие параметры — ниже.</small></div>'
            )
            if current_event_name:
                put_html(
                    '<div class="event-map-current-event">'
                    '<span>Название события</span>'
                    f'<strong>{escape(current_event_name)}</strong>'
                    '<small>Определено автоматически по текущему Event artifact.</small>'
                    '</div>'
                )
        for name in EVENT_MAP_PRIMARY_GROUPS:
            self._render_named_group(task, name, group_map, config)
        self._render_advanced(
            task=task,
            title="Расширенные настройки карты",
            description="Подводный флот, контроль HP и приоритет вражеских флотов.",
            names=EVENT_MAP_ADVANCED_GROUPS,
            group_map=group_map,
            config=config,
        )

    def _render_event_shop_layout(self, *, task, group_map, config) -> None:
        with use_scope("groups"):
            put_scope("group_EventShopPlan")
            if "Scheduler" in group_map:
                put_scope("group_Scheduler")
        self._render_named_group(task, "Scheduler", group_map, config)
        self._render_advanced(
            task=task,
            title="Расширенные настройки — автоматизация магазина",
            description="Ручной DSL и редкие SSR/UR-сценарии.",
            names=("EventShop",),
            group_map=group_map,
            config=config,
        )
        with use_scope("group_EventShopPlan", clear=True):
            self._render_event_shop_plan(config)

    @use_scope("content", clear=True)
    def _alas_set_event_group(self, task: str) -> None:
        config = self.alas_config.read_file(self.alas_name)

        # Сначала создаём новый shell страницы, затем выполняем более дорогой artifact lookup.
        # Поэтому пользователь не видит старую форму, ожидая current-event resolution.
        self.init_menu(name=task)
        self.set_title(t(f"Task.{task}.name"))
        put_scope("_groups", [put_none(), put_scope("groups"), put_scope("navigator")])
        self._mark_event_page(task)

        task_args: Mapping[str, Any] = self.ALAS_ARGS[task]
        current_event_name: str | None = None
        if task in EVENT_MAP_TASKS:
            task_args, config, current_event_name = self._prepare_event_map_args(
                task, config
            )
        group_map = self._event_group_map(dict(task_args))

        if task in EVENT_MAP_TASKS:
            self._event_plan_active_task = task
            self._render_event_map_layout(
                task=task,
                group_map=group_map,
                config=config,
                current_event_name=current_event_name,
            )
        elif task == "EventShop":
            self._event_plan_active_task = task
            self._render_event_shop_layout(
                task=task, group_map=group_map, config=config
            )

    def alas_set_group(self, task: str) -> None:
        if task not in EVENT_LAYOUT_TASKS:
            return super().alas_set_group(task)
        return self._alas_set_event_group(task)
