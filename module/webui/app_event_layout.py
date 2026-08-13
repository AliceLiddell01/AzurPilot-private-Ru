"""Modern presentation layer for Event pages without changing runtime task contracts."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from functools import partial
from html import escape
from typing import Any

from module.config.time_sentinel import DEFAULT_TIME_TEXT, is_default_time
from module.webui.app_dependencies import (
    close_popup,
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
    put_table,
    run_js,
    t,
    toast,
    use_scope,
)
from module.webui.app_event_planner import EventPlannerMixin
from module.webui.app_helpers import is_demo_mode
from module.webui.event_assets import event_asset_url
from module.webui.event_plan import (
    shop_plan_total,
)

EVENT_MAP_TASKS = frozenset({"Event", "Event2", "Event3"})
EVENT_LAYOUT_TASKS = EVENT_MAP_TASKS | {"EventGeneral", "EventShop"}
EVENT_MAP_PRIMARY_GROUPS = (
    "Scheduler",
    "Campaign",
    "StopCondition",
    "Fleet",
    "Emotion",
)
EVENT_MAP_ADVANCED_GROUPS = ("Submarine", "HpControl", "EnemyPriority")
_DISABLED_EVENT_TIME = DEFAULT_TIME_TEXT

_TARGET_PT = "event_modern_target_pt"


class EventLayoutMixin(EventPlannerMixin):
    """Render Event pages as a compact dashboard with progressive disclosure."""

    @staticmethod
    def _event_group_map(task_args: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
        return {
            group[0]: (group, args) for group, args in deep_iter(task_args, depth=1)
        }

    @staticmethod
    def _fmt(value: Any) -> str:
        try:
            return f"{int(value):,}".replace(",", " ")
        except TypeError, ValueError:
            return str(value)

    @staticmethod
    def _time_label(value: Any) -> str:
        if is_default_time(value):
            return "Без ограничения"
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d %H:%M:%S")
        return str(value or "").strip() or "Без ограничения"

    @staticmethod
    def _event_write_allowed() -> bool:
        if not is_demo_mode():
            return True
        toast(
            "В демонстрационном режиме изменение настроек ивента отключено.",
            color="warning",
        )
        return False

    @staticmethod
    def _source_badge(plan: Mapping[str, Any]) -> str:
        source = plan.get("event", {}).get("source", {})
        kind = (
            str(source.get("kind") or "manual")
            if isinstance(source, Mapping)
            else "manual"
        )
        status = (
            str(source.get("status") or "unsupported")
            if isinstance(source, Mapping)
            else "unsupported"
        )
        if kind == "azurlane_lua":
            label = {
                "verified": "Игровые данные · проверено",
                "partial": "Игровые данные · частично",
                "unsupported": "Игровые данные · не поддержано",
            }.get(status, "Игровые данные")
            tone = "success" if status == "verified" else "warning"
        else:
            label, tone = "Источник не выбран", "neutral"
        return f'<span class="event-status-pill event-status-{tone}">{escape(label)}</span>'

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
        """Remove Event-only DOM state whenever another WebUI surface becomes active."""
        if name not in EVENT_LAYOUT_TASKS:
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
        existing = [name for name in names if name in group_map]
        if not existing:
            return
        key = "-".join(name.lower() for name in existing)
        body_id = f"event-advanced-{task.lower()}-{key}-body"
        with use_scope("groups"):
            put_html(
                f'<details class="event-advanced-details"><summary><span>{escape(title)}</span>'
                '<span class="event-details-chevron"></span></summary>'
                f'<div class="event-advanced-description">{escape(description)}</div>'
                f'<div id="{escape(body_id)}" class="event-advanced-body"></div></details>'
            )
        rendered = [
            name
            for name in existing
            if self._render_named_group(task, name, group_map, config, False)
        ]
        if not rendered:
            return
        ids = [f"pywebio-scope-group_{name}" for name in rendered]
        run_js(
            """
(() => {
  const body = document.getElementById(%s);
  if (!body) return;
  for (const id of %s) {
    const node = document.getElementById(id);
    if (node && node.parentNode !== body) body.appendChild(node);
  }
})();
"""
            % (json.dumps(body_id), json.dumps(ids))
        )

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
        except TypeError, ValueError:
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

    def _render_modern_sources(self, plan: Mapping[str, Any]) -> None:
        labels = {
            "daily": "Ежедневные задания",
            "weekly": "Еженедельные задания",
            "one_time": "Разовые задания",
            "first_clear": "Первое прохождение",
            "daily_first_clear": "Ежедневное первое прохождение",
            "repeatable_map_clear": "Повторяемый фарм карт",
            "challenge": "Испытания",
            "unknown": "Не классифицировано",
        }
        sources = list(plan.get("pt_sources", []))
        for kind, title in labels.items():
            rows = [item for item in sources if item.get("kind") == kind]
            if not rows:
                continue
            cards = "".join(
                '<article class="event-source-card">'
                f'<span class="event-source-kind">{escape(title)}</span>'
                f'<strong>{escape(str(item.get("name") or item.get("id")))}</strong>'
                f'<b>{escape(self._fmt(item["points"]) if item.get("points") is not None else "Нет данных")} PT</b>'
                '<small>Автостатус пока недоступен</small></article>'
                for item in rows
            )
            put_html(
                f'<div class="event-subsection-heading"><span>{title}</span>'
                f'<span class="event-subsection-count">{len(rows)}</span></div>'
                f'<div class="event-source-grid">{cards}</div>'
            )

    def _render_event_plan_general(self, config: Mapping[str, Any]) -> None:
        self._event_plan_active_task = "EventGeneral"
        plan = self._event_plan()
        event = plan["event"]
        target = int(deep_get(config, "EventGeneral.EventGeneral.PtLimit", 0) or 0)
        time_limit = deep_get(
            config, "EventGeneral.EventGeneral.TimeLimit", _DISABLED_EVENT_TIME
        )
        shop_total = shop_plan_total(plan)
        planning_target = max(target, shop_total)
        progress_data = plan.get("progress", {})
        current_pt = (
            progress_data.get("current_pt")
            if isinstance(progress_data, Mapping)
            else None
        )
        current_source = {
            "observed": f"Автоматически из OCR ({progress_data.get('observed_at')})",
            "stale": "OCR-наблюдение устарело",
            "unavailable": "OCR PT ещё не записан",
        }.get(
            str(progress_data.get("status") or "unavailable"), "Наблюдение недоступно"
        )
        remaining_pt = (
            max(planning_target - current_pt, 0)
            if isinstance(current_pt, int)
            else None
        )
        progress = (
            max(0, min(100, round(current_pt * 100 / planning_target)))
            if planning_target > 0 and isinstance(current_pt, int)
            else 0
        )
        farm_end = escape(str(event.get("farm_end") or "Не задано"))
        farm_start = escape(str(event.get("farm_start") or "Не задано"))
        shop_end = escape(str(event.get("shop_end") or "Не задано"))
        source = event.get("source", {})
        source = source if isinstance(source, Mapping) else {}
        revision = str(source.get("revision") or "")
        currencies = "".join(
            '<span class="event-currency-chip">'
            f'<img src="{escape(event_asset_url(item.get("asset") if isinstance(item, Mapping) else None))}" alt="">'
            f'<span>{escape(str(item.get("name") or item.get("id") or "Валюта"))}</span></span>'
            for item in plan.get("currencies", [])
            if isinstance(item, Mapping)
        )
        put_html(f"""
<section class="event-dashboard-hero">
  <div class="event-hero-copy"><div class="event-eyebrow">Текущий ивент · {escape(str(event.get("server") or "EN"))}</div>
  <h3>{escape(str(event.get("name") or "Текущий ивент не задан"))}</h3>
  <div class="event-hero-meta"><span>Фарм <strong>{farm_start}</strong> — <strong>{farm_end}</strong></span><span>Магазин до <strong>{shop_end}</strong></span>{self._source_badge(plan)}</div>
  <div class="event-provenance-row"><span>{escape(str(source.get("provider") or "AzurLaneLuaScripts"))}</span><code>{escape(revision[:12] if revision else "revision unavailable")}</code>{currencies}</div></div>
  <div class="event-metrics-grid">
    <div class="event-metric-card event-metric-accent"><span class="event-metric-label">Текущий PT</span><strong>{self._fmt(current_pt) if current_pt is not None else "Нет данных"}</strong><small>{escape(current_source)}</small></div>
    <div class="event-metric-card"><span class="event-metric-label">Автостоп</span><strong>{self._fmt(target) + " PT" if target else "Выключен"}</strong><small>{escape(self._time_label(time_limit))}</small></div>
    <div class="event-metric-card"><span class="event-metric-label">План магазина</span><strong>{self._fmt(shop_total)} PT</strong><small>В расчёте автоматически</small></div>
    <div class="event-metric-card"><span class="event-metric-label">Осталось нафармить</span><strong>{self._fmt(remaining_pt) + " PT" if remaining_pt is not None else "Нет данных"}</strong><small>Автопрогноз источников недоступен</small></div>
  </div>
  <div class="event-progress-label"><span>Прогресс к расчётной цели</span><strong>{str(progress) + "%" if current_pt is not None else "—"}</strong></div>
  <div class="event-progress-track"><span style="width:{progress}%"></span></div>
</section>""")
        put_row(
            [
                put_button(
                    "Настроить цель фарма",
                    onclick=self._settings_popup,
                    color="primary",
                )
            ],
            size="auto",
        )
        if event.get("source", {}).get("kind") == "manual_empty":
            put_row(
                [
                    put_button(
                        "Использовать сгенерированный источник",
                        onclick=self._activate_generated_event_source,
                        color="primary",
                    )
                ],
                size="auto",
            )
        finding_items = list(plan.get("source_findings", [])) + list(
            plan.get("observation", {}).get("findings", [])
        )
        findings = [
            [
                str(item.get("severity") or ""),
                str(item.get("code") or ""),
                str(item.get("path") or ""),
                str(item.get("message") or ""),
            ]
            for item in finding_items
            if isinstance(item, Mapping)
        ]
        diagnostics = [
            put_table(
                [
                    ["Статус", str(plan.get("source_status") or "unsupported")],
                    ["Repository", str(source.get("repository") or "")],
                    ["Revision", str(source.get("revision") or "")],
                ],
                header=["Источник", "Значение"],
            )
        ]
        if findings:
            diagnostics.append(
                put_table(
                    findings,
                    header=["Severity", "Code", "Path", "Диагностика"],
                )
            )
        put_collapse("Источник и диагностика", diagnostics, open=False)

        put_html(
            '<div class="event-section-heading"><span>Источники PT</span><small>Только источниковые факты; ручные статусы не используются.</small></div>'
        )
        self._render_modern_sources(plan)

        put_html(
            '<div class="event-section-heading"><span>Этапы фарма</span><small>Прогноз проходов пересчитывается автоматически.</small></div>'
        )
        stage_cards = []
        for stage in plan.get("stages", []):
            points = stage.get("points")
            runs = (
                "—"
                if not isinstance(points, int) or points <= 0 or remaining_pt is None
                else self._fmt((remaining_pt + points - 1) // points)
            )
            status = (
                "Наблюдение синхронизировано"
                if stage.get("observation_status") == "observed"
                else "Автостатус пока недоступен"
            )
            compiler_status = str(stage.get("source_status") or "unsupported")
            runtime_label = (
                "Runtime eligible"
                if stage.get("runtime_eligible")
                else "Runtime blocked"
            )
            stage_cards.append(
                f'<article class="event-farm-card"><div class="event-farm-card-head"><strong>{escape(str(stage["name"]))}</strong><span>{escape(status)}</span></div>'
                f'<div class="event-map-identity">Map {escape(str(stage.get("id") or ""))} · {escape(compiler_status)} · {escape(runtime_label)}</div>'
                f'<div class="event-farm-facts"><span><small>PT</small><b>{escape(self._fmt(points) if points is not None else "Нет данных")}</b></span>'
                f"<span><small>Нефть</small><b>{escape(self._fmt(stage.get('oil')) if stage.get('oil') is not None else 'Нет данных')}</b></span>"
                f"<span><small>Монеты</small><b>{escape(self._fmt(stage.get('coin')) if stage.get('coin') is not None else 'Нет данных')}</b></span>"
                f"<span><small>Звёзды</small><b>{escape(str(stage.get('stars')) if stage.get('stars') is not None else 'Нет данных')}</b></span>"
                f"<span><small>Проходы</small><b>{escape(runs)}</b></span></div></article>"
            )
        if stage_cards:
            put_html(f'<div class="event-farm-grid">{"".join(stage_cards)}</div>')
        else:
            put_html(
                '<div class="event-empty-card"><strong>Этапы отсутствуют в datamine artifact</strong></div>'
            )
        milestone_cards = []
        for milestone in plan.get("milestones", []):
            rewards = "".join(
                f'<span class="event-reward-chip"><img src="{escape(event_asset_url(reward.get("asset") if isinstance(reward, Mapping) else None))}" alt=""><span>{escape(str(reward.get("name") or reward.get("reward_id")))} × {escape(self._fmt(reward.get("amount")))}</span></span>'
                for reward in milestone.get("rewards", [])
                if isinstance(reward, Mapping)
            )
            threshold = int(milestone.get("threshold", 0) or 0)
            if isinstance(current_pt, int):
                milestone_status = (
                    "Порог достигнут; получение не подтверждено"
                    if current_pt >= threshold
                    else f"До порога: {self._fmt(threshold - current_pt)} PT"
                )
            else:
                milestone_status = "Прогресс недоступен"
            milestone_cards.append(
                '<article class="event-milestone-card">'
                f'<div><small>Порог</small><strong>{escape(self._fmt(threshold))} PT</strong></div>'
                f'<div class="event-milestone-rewards">{rewards or "<span>Нет данных</span>"}</div>'
                f'<small>{escape(milestone_status)}</small></article>'
            )
        if milestone_cards:
            put_html(
                '<div class="event-section-heading"><span>Награды за накопление PT</span>'
                f'<small>{len(milestone_cards)} порогов из datamine</small></div>'
                f'<div class="event-milestone-grid">{"".join(milestone_cards)}</div>'
            )

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
            sum(int(item["price"]) * int(item["remaining"]) for item in items)
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
  <div><span>Ваш план</span><strong>{self._fmt(total)}</strong><small>{len(selected)} позиций</small></div>
  <div><span>Осталось по scan</span><strong>{self._fmt(observed_total) if observed_total is not None else "Нет данных"}</strong><small>{"Полный snapshot" if observed_known else "Наблюдение недоступно"}</small></div>
</div></section>""")
        put_scope("event_shop_safety_status")
        if items:
            put_scope("event_shop_grid")
            card_scope_ids = []
            with use_scope("event_shop_grid"):
                for index, item in enumerate(items):
                    identity = self._shop_item_identity(item)
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
                            f'<span class="event-shop-rarity event-rarity-{escape(str(item.get("rarity") or "unknown"))}">Rarity {escape(str(item.get("rarity") if item.get("rarity") is not None else "—"))}</span>'
                            f'<h4>{escape(str(item.get("name") or "Без названия"))}</h4>'
                            f'<small>{escape(str(item.get("category") or "unknown"))} · bundle ×{escape(self._fmt(item.get("amount", 1)))}</small>'
                            '<div class="event-shop-price">'
                            f'<img src="{escape(event_asset_url(currency.get("asset") if isinstance(currency, Mapping) else None))}" alt="">'
                            f'<strong>{escape(self._fmt(item.get("price")))}</strong></div></div>'
                            '<div class="event-shop-observation">'
                            f'<span>{escape(observation_label)}</span>'
                            f'<small>Куплено: {escape(self._fmt(item.get("purchased")) if item.get("purchased") is not None else "Нет данных")} · Осталось: {escape(self._fmt(item.get("remaining")) if item.get("remaining") is not None else "Нет данных")}</small></div>'
                            '<div class="event-shop-desired">'
                            f'<span>Цель</span><strong>{escape(self._fmt(item.get("selected")))} / {escape(self._fmt(item.get("stock")))}</strong>'
                            f'<small>Стоимость: {escape(self._fmt(int(item.get("price", 0) or 0) * int(item.get("selected", 0) or 0)))}</small></div>'
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

    def _render_event_general_layout(self, *, task, group_map, config) -> None:
        with use_scope("groups"):
            put_scope("group_EventPlan")
        with use_scope("group_EventPlan", clear=True):
            self._render_event_plan_general(config)
        self._render_advanced(
            task=task,
            title="Расширенные настройки — баланс задач",
            description="Автоматический баланс и переключение на другую задачу.",
            names=("TaskBalancer",),
            group_map=group_map,
            config=config,
        )

    def _render_event_map_layout(self, *, task, group_map, config) -> None:
        with use_scope("groups"):
            put_html(
                '<div class="event-map-intro"><span>Ивентовая карта</span><small>Основное — на виду, редкие параметры — ниже.</small></div>'
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
        with use_scope("group_EventShopPlan", clear=True):
            self._render_event_shop_plan(config)
        self._render_named_group(task, "Scheduler", group_map, config)
        self._render_advanced(
            task=task,
            title="Расширенные настройки — автоматизация магазина",
            description="Ручной DSL и редкие SSR/UR-сценарии.",
            names=("EventShop",),
            group_map=group_map,
            config=config,
        )

    @use_scope("content", clear=True)
    def _alas_set_event_group(self, task: str) -> None:
        config = self.alas_config.read_file(self.alas_name)
        self.init_menu(name=task)
        self.set_title(t(f"Task.{task}.name"))
        put_scope("_groups", [put_none(), put_scope("groups"), put_scope("navigator")])
        self._mark_event_page(task)
        group_map = self._event_group_map(self.ALAS_ARGS[task])
        if task == "EventGeneral":
            self._event_plan_active_task = task
            self._render_event_general_layout(
                task=task, group_map=group_map, config=config
            )
        elif task in EVENT_MAP_TASKS:
            self._event_plan_active_task = task
            self._render_event_map_layout(task=task, group_map=group_map, config=config)
        elif task == "EventShop":
            self._event_plan_active_task = task
            self._render_event_shop_layout(
                task=task, group_map=group_map, config=config
            )

    def alas_set_group(self, task: str) -> None:
        if task not in EVENT_LAYOUT_TASKS:
            self._unmark_event_page()
            return super().alas_set_group(task)
        return self._alas_set_event_group(task)
