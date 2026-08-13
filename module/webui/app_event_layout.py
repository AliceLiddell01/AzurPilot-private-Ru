"""Modern presentation layer for Event pages without changing runtime task contracts."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime
from functools import partial
from html import escape
from typing import Any, Dict, Iterable, Mapping, Tuple

from module.config.time_sentinel import DEFAULT_TIME_TEXT, is_default_time
from module.webui.app_dependencies import (
    close_popup,
    current_time,
    deep_get,
    deep_iter,
    load_event_calculator,
    logger,
    pin,
    popup,
    put_button,
    put_buttons,
    put_collapse,
    put_html,
    put_input,
    put_none,
    put_row,
    put_scope,
    put_select,
    put_table,
    put_text,
    run_js,
    t,
    toast,
    use_scope,
)
from module.webui.app_event_planner import EventPlannerMixin, _EVENT_PLAN_MUTATION_LOCK
from module.webui.app_helpers import is_demo_mode
from module.webui.event_plan import estimate_stage_runs, event_farm_summary, shop_plan_total

EVENT_MAP_TASKS = frozenset({"Event", "Event2", "Event3"})
EVENT_LAYOUT_TASKS = EVENT_MAP_TASKS | {"EventGeneral", "EventShop"}
EVENT_MAP_PRIMARY_GROUPS = ("Scheduler", "Campaign", "StopCondition", "Fleet", "Emotion")
EVENT_MAP_ADVANCED_GROUPS = ("Submarine", "HpControl", "EnemyPriority")
_DISABLED_EVENT_TIME = DEFAULT_TIME_TEXT

_NAME = "event_modern_name"
_FARM_END = "event_modern_farm_end"
_SHOP_END = "event_modern_shop_end"
_PT_MODE = "event_modern_pt_mode"
_CURRENT_PT = "event_modern_current_pt"
_TARGET_PT = "event_modern_target_pt"
_SOURCE_KIND = "event_modern_source_kind"
_SOURCE_NAME = "event_modern_source_name"
_SOURCE_PT = "event_modern_source_pt"


class EventLayoutMixin(EventPlannerMixin):
    """Render Event pages as a compact dashboard with progressive disclosure."""

    @staticmethod
    def _event_group_map(task_args: Dict[str, Any]) -> Dict[str, Tuple[Any, Any]]:
        return {group[0]: (group, args) for group, args in deep_iter(task_args, depth=1)}

    @staticmethod
    def _fmt(value: Any) -> str:
        try:
            return f"{int(value):,}".replace(",", " ")
        except (TypeError, ValueError):
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
        toast("В демонстрационном режиме изменение настроек ивента отключено.", color="warning")
        return False

    @staticmethod
    def _source_badge(plan: Mapping[str, Any]) -> str:
        source = plan.get("event", {}).get("source", {})
        kind = str(source.get("kind") or "manual") if isinstance(source, Mapping) else "manual"
        verified = bool(source.get("verified")) if isinstance(source, Mapping) else False
        if kind == "legacy_bwiki":
            label, tone = "Legacy BWiki", "warning"
        elif kind == "azurlane_lua":
            label, tone = "Игровые данные", "success" if verified else "neutral"
        elif verified:
            label, tone = "Проверено вручную", "success"
        else:
            label, tone = "Локальный план", "neutral"
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

    def _render_advanced(self, *, task: str, title: str, description: str, names: Iterable[str], group_map, config) -> None:
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
        rendered = [name for name in existing if self._render_named_group(task, name, group_map, config, False)]
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
        plan = self._event_plan()
        event, progress = plan["event"], plan["progress"]
        target = int(deep_get(config, "EventGeneral.EventGeneral.PtLimit", 0) or 0)
        popup(
            "Настроить ивент",
            [
                put_html('<span class="event-modern-dialog-marker"></span>'),
                put_input(_NAME, label="Название ивента", value=event.get("name", "")),
                put_row([
                    put_input(_FARM_END, label="Окончание фарма", value=event.get("farm_end", ""), placeholder="YYYY-MM-DD HH:MM:SS"),
                    put_input(_SHOP_END, label="Окончание магазина", value=event.get("shop_end", ""), placeholder="YYYY-MM-DD HH:MM:SS"),
                ], size="1fr 1fr"),
                put_row([
                    put_select(_PT_MODE, label="Источник текущего PT", value=progress.get("pt_mode", "auto"), options=[
                        {"label": "Автоматически из последнего OCR", "value": "auto"},
                        {"label": "Вручную", "value": "manual"},
                    ]),
                    put_input(_CURRENT_PT, type="number", min=0, label="Текущий PT — ручной fallback", value=progress.get("current_pt", 0)),
                ], size="1fr 1fr"),
                put_input(_TARGET_PT, type="number", min=0, label="Автостоп по PT", value=target,
                          help_text="0 — без ограничения. План магазина учитывается в прогнозе, но не меняет balance-based PT-автостоп."),
                put_row([
                    put_button("Сохранить", onclick=self._save_settings_popup, color="primary"),
                    put_button("Отмена", onclick=close_popup, color="off"),
                ], size="auto auto"),
            ],
        )

    def _save_settings_popup(self) -> None:
        if not self._event_write_allowed():
            return
        name = str(pin[_NAME] or "").strip()
        farm_end = str(pin[_FARM_END] or "").strip()
        shop_end = str(pin[_SHOP_END] or "").strip()
        pt_mode = str(pin[_PT_MODE] or "auto").lower()
        try:
            current_pt = int(pin[_CURRENT_PT] or 0)
            target_pt = int(pin[_TARGET_PT] or 0)
        except (TypeError, ValueError):
            current_pt = target_pt = -1
        if current_pt < 0 or target_pt < 0:
            toast("PT не может быть отрицательным", color="warning")
            return
        if not self._valid_datetime_text(farm_end) or not self._valid_datetime_text(shop_end):
            toast("Дата должна быть в формате YYYY-MM-DD или YYYY-MM-DD HH:MM:SS", color="warning")
            return
        if pt_mode not in {"auto", "manual"}:
            pt_mode = "auto"

        with _EVENT_PLAN_MUTATION_LOCK:
            previous_plan = self._event_plan()
            plan = deepcopy(previous_plan)
            event = plan["event"]
            old_farm_end = str(event.get("farm_end") or "")
            old_shop_end = str(event.get("shop_end") or "")
            farm_end_changed = farm_end != old_farm_end
            shop_end_changed = shop_end != old_shop_end
            source = event.get("source", {})
            source_verified = bool(source.get("verified")) if isinstance(source, Mapping) else False

            event.update({"name": name, "farm_end": farm_end, "shop_end": shop_end})
            if farm_end_changed:
                event["source"] = {
                    "kind": "manual",
                    "verified": True,
                    "updated_at": "",
                    "revision": "",
                }
            elif shop_end_changed and not source_verified:
                event["source"] = {
                    "kind": "manual",
                    "verified": False,
                    "updated_at": "",
                    "revision": "",
                }
            plan["progress"].update({"current_pt": current_pt, "pt_mode": pt_mode})

            updates = {"EventGeneral.EventGeneral.PtLimit": target_pt}
            verified = bool(event.get("source", {}).get("verified"))
            time_applied = False
            if not farm_end:
                updates["EventGeneral.EventGeneral.TimeLimit"] = _DISABLED_EVENT_TIME
                time_applied = True
            elif verified:
                updates["EventGeneral.EventGeneral.TimeLimit"] = self._config_datetime(farm_end)
                time_applied = True

            if not self._event_plan_write(plan, ""):
                return
            try:
                self._event_config_update(updates)
            except Exception as exc:
                logger.exception(exc)
                rolled_back = self._event_plan_write(previous_plan, "")
                detail = "Локальный план восстановлен." if rolled_back else "Восстановить локальный план не удалось."
                toast(
                    f"Не удалось сохранить runtime-настройки ивента: {exc} {detail}",
                    color="error",
                    duration=10,
                )
                return

        close_popup()
        if time_applied:
            toast("План и автостоп синхронизированы", color="success")
        else:
            toast(
                "План и PT-автостоп сохранены. Окончание фарма из неподтверждённого "
                "источника не применено к runtime; измените дату вручную, чтобы подтвердить её.",
                color="warning",
                duration=8,
            )
        self._refresh_event_plan_page()


    def _add_source_popup(self) -> None:
        popup(
            "Добавить источник PT",
            [
                put_html('<span class="event-modern-dialog-marker"></span>'),
                put_select(
                    _SOURCE_KIND,
                    label="Тип",
                    value="daily",
                    options=[
                        {"label": "Ежедневный источник", "value": "daily"},
                        {"label": "Дополнительно за день", "value": "extra"},
                    ],
                ),
                put_input(_SOURCE_NAME, label="Название"),
                put_input(_SOURCE_PT, type="number", min=1, value=1, label="PT за день"),
                put_row(
                    [
                        put_button("Добавить", onclick=self._save_source_popup, color="primary"),
                        put_button("Отмена", onclick=close_popup, color="off"),
                    ],
                    size="auto auto",
                ),
            ],
        )

    def _save_source_popup(self) -> None:
        kind = str(pin[_SOURCE_KIND] or "daily")
        name = str(pin[_SOURCE_NAME] or "").strip()
        try:
            points = int(pin[_SOURCE_PT] or 0)
        except (TypeError, ValueError):
            points = 0
        if kind not in {"daily", "extra"} or not name or points <= 0:
            toast("Укажите тип, название и положительное количество PT", color="warning")
            return
        def mutation(plan):
            rows = [item for item in plan[kind] if item["name"] != name]
            rows.append(
                {"name": name, "points": points, "skip": False, "completed_date": ""}
            )
            plan[kind] = rows

        if self._event_plan_mutate(mutation, f"Источник «{name}» добавлен"):
            close_popup()
            self._refresh_event_plan_page()

    def _render_modern_sources(self, plan: Mapping[str, Any], kind: str) -> None:
        title = "Ежедневные источники" if kind == "daily" else "Дополнительно за день"
        rows = list(plan.get(kind, []))
        put_html(
            f'<div class="event-subsection-heading"><span>{title}</span>'
            f'<span class="event-subsection-count">{len(rows)}</span></div>'
        )
        if not rows:
            put_html('<div class="event-inline-empty">Пока ничего не добавлено.</div>')
            return
        today = current_time().date().isoformat()
        table = []
        for item in rows:
            if item.get("skip"):
                state = "Пропускается"
            elif item.get("completed_date") == today:
                state = "Получено сегодня"
            else:
                state = "Ожидается"
            table.append(
                [
                    item["name"],
                    self._fmt(item["points"]),
                    state,
                    put_buttons(
                        [
                            {"label": "Получено", "value": "done", "color": "off"},
                            {"label": "Пропуск", "value": "skip", "color": "off"},
                            {"label": "Удалить", "value": "delete", "color": "off"},
                        ],
                        onclick=partial(
                            self._point_source_action,
                            kind,
                            item["name"],
                            item["points"],
                        ),
                    ),
                ]
            )
        put_table(table, header=["Источник", "PT/день", "Сегодня", ""])

    def _render_event_plan_general(self, config: Mapping[str, Any]) -> None:
        self._event_plan_active_task = "EventGeneral"
        plan = self._event_plan()
        event = plan["event"]
        target = int(deep_get(config, "EventGeneral.EventGeneral.PtLimit", 0) or 0)
        time_limit = deep_get(config, "EventGeneral.EventGeneral.TimeLimit", _DISABLED_EVENT_TIME)
        shop_total = shop_plan_total(plan)
        planning_target = max(target, shop_total)
        current_pt, current_source = self._current_pt_for_plan(config, plan)
        forecast = event_farm_summary(plan, planning_target, current_pt=current_pt, today=current_time().replace(tzinfo=None, microsecond=0))
        progress = max(0, min(100, round(current_pt * 100 / planning_target))) if planning_target > 0 else 0
        farm_end = escape(str(event.get("farm_end") or "Не задано"))
        shop_end = escape(str(event.get("shop_end") or "Не задано"))
        put_html(f"""
<section class="event-dashboard-hero">
  <div class="event-hero-copy"><div class="event-eyebrow">Текущий ивент · {escape(str(event.get('server') or 'EN'))}</div>
  <h3>{escape(str(event.get('name') or 'Текущий ивент не задан'))}</h3>
  <div class="event-hero-meta"><span>Фарм до <strong>{farm_end}</strong></span><span>Магазин до <strong>{shop_end}</strong></span>{self._source_badge(plan)}</div></div>
  <div class="event-metrics-grid">
    <div class="event-metric-card event-metric-accent"><span class="event-metric-label">Текущий PT</span><strong>{self._fmt(current_pt)}</strong><small>{escape(current_source)}</small></div>
    <div class="event-metric-card"><span class="event-metric-label">Автостоп</span><strong>{self._fmt(target) + ' PT' if target else 'Выключен'}</strong><small>{escape(self._time_label(time_limit))}</small></div>
    <div class="event-metric-card"><span class="event-metric-label">План магазина</span><strong>{self._fmt(shop_total)} PT</strong><small>В расчёте автоматически</small></div>
    <div class="event-metric-card"><span class="event-metric-label">Осталось нафармить</span><strong>{self._fmt(forecast['farm_required_pt'])} PT</strong><small>Ежедневные: {self._fmt(forecast['recurring_pt'])} PT</small></div>
  </div>
  <div class="event-progress-label"><span>Прогресс к расчётной цели</span><strong>{progress}%</strong></div>
  <div class="event-progress-track"><span style="width:{progress}%"></span></div>
</section>""")
        put_row([put_button("Настроить ивент", onclick=self._settings_popup, color="primary")], size="auto")

        put_html('<div class="event-section-heading"><span>Источники PT</span><small>Учитываются в прогнозе автоматически.</small></div>')
        self._render_modern_sources(plan, "daily")
        self._render_modern_sources(plan, "extra")
        put_row([put_button("Добавить источник PT", onclick=self._add_source_popup, color="off")], size="auto")

        put_html('<div class="event-section-heading"><span>Этапы фарма</span><small>Прогноз проходов пересчитывается автоматически.</small></div>')
        stage_rows = []
        for stage in estimate_stage_runs(plan, forecast["farm_required_pt"]):
            stage_rows.append([stage["name"], self._fmt(stage["points"]), self._fmt(stage["runs"]),
                               put_button("Удалить", onclick=partial(self._delete_stage, stage["name"], stage["points"]), color="off")])
        if stage_rows:
            put_table(stage_rows, header=["Этап", "PT/проход", "Нужно проходов", ""])
        else:
            put_html('<div class="event-empty-card"><strong>Этапы ещё не добавлены</strong><small>Добавьте карту и PT за прохождение.</small></div>')
        put_row([put_button("Добавить этап", onclick=self._add_stage_popup, color="off")], size="auto")
        put_collapse("Обслуживание локального плана", [
            put_row([
                put_button("Импортировать legacy BWiki", onclick=self._import_legacy_bwiki_cache, color="off"),
                put_button("Очистить локальный план", onclick=self._clear_event_plan, color="off"),
            ], size="auto auto")
        ], open=False)

    def _render_event_shop_plan(self, config: Mapping[str, Any]) -> None:
        self._event_plan_active_task = "EventShop"
        plan = self._event_plan()
        items = list(plan["shop_items"])
        total = shop_plan_total(plan)
        selected = [item for item in items if int(item.get("selected", 0) or 0) > 0]
        put_html(f"""
<section class="event-shop-hero"><div><div class="event-eyebrow">План покупок</div>
<h3>{escape(str(plan['event'].get('name') or 'Текущий ивент не задан'))}</h3>
<p>Безопасный план синхронизируется с EventShop автоматически.</p></div>
<div class="event-shop-total"><span>Нужно PT</span><strong>{self._fmt(total)}</strong><small>{len(selected)} позиций выбрано</small></div></section>""")
        put_scope("event_shop_safety_status")
        put_row([put_button("Добавить товар", onclick=self._add_shop_item_popup, color="primary")], size="auto")
        if items:
            rows = []
            for item in items:
                identity = self._shop_item_identity(item)
                rows.append([item["name"], self._fmt(item["price"]), f'{self._fmt(item["selected"])} / {self._fmt(item["stock"])}',
                             self._fmt(int(item["price"]) * int(item["selected"])),
                             put_buttons([{"label": "Количество", "value": "edit", "color": "off"}, {"label": "Удалить", "value": "delete", "color": "off"}],
                                         onclick=lambda action, key=identity: self._shop_quantity_popup(key) if action == "edit" else self._delete_shop_item(key))])
            put_table(rows, header=["Товар", "Цена", "Количество", "Итого PT", ""])
        else:
            put_html('<div class="event-empty-card"><strong>План магазина пуст</strong><small>Добавьте первый товар или импортируйте legacy-кэш.</small></div>')
        put_collapse("Импорт и служебные действия", [
            put_button("Импортировать legacy BWiki", onclick=self._import_legacy_bwiki_cache, color="off")
        ], open=False)

    def _refresh_legacy_bwiki_cache(self) -> None:
        if not self._event_write_allowed():
            return
        data = load_event_calculator(force_refresh=True)
        error = data.get("error")
        if error:
            toast(f"Не удалось обновить legacy BWiki: {error}", color="warning", duration=8)
            return
        toast(f"Legacy-кэш BWiki обновлён: {data.get('event_name') or 'данные ивента'}", color="success")

    def _render_event_general_layout(self, *, task, group_map, config) -> None:
        with use_scope("groups"):
            put_scope("group_EventPlan")
        with use_scope("group_EventPlan", clear=True):
            self._render_event_plan_general(config)
        self._render_advanced(task=task, title="Расширенные настройки — баланс задач",
                              description="Автоматический баланс и переключение на другую задачу.",
                              names=("TaskBalancer",), group_map=group_map, config=config)
        with use_scope("groups"):
            put_collapse("Резервный источник — BWiki (legacy)", [
                put_row([
                    put_button("Обновить legacy-кэш", onclick=self._refresh_legacy_bwiki_cache, color="off"),
                    put_button("Импортировать в план", onclick=self._import_legacy_bwiki_cache, color="off"),
                ], size="auto auto")
            ], open=False)

    def _render_event_map_layout(self, *, task, group_map, config) -> None:
        with use_scope("groups"):
            put_html('<div class="event-map-intro"><span>Ивентовая карта</span><small>Основное — на виду, редкие параметры — ниже.</small></div>')
        for name in EVENT_MAP_PRIMARY_GROUPS:
            self._render_named_group(task, name, group_map, config)
        self._render_advanced(task=task, title="Расширенные настройки карты",
                              description="Подводный флот, контроль HP и приоритет вражеских флотов.",
                              names=EVENT_MAP_ADVANCED_GROUPS, group_map=group_map, config=config)

    def _render_event_shop_layout(self, *, task, group_map, config) -> None:
        self._render_named_group(task, "Scheduler", group_map, config)
        with use_scope("groups"):
            put_scope("group_EventShopPlan")
        with use_scope("group_EventShopPlan", clear=True):
            self._render_event_shop_plan(config)
        self._render_advanced(task=task, title="Расширенные настройки — автоматизация магазина",
                              description="Ручной DSL и редкие SSR/UR-сценарии.", names=("EventShop",), group_map=group_map, config=config)

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
            self._render_event_general_layout(task=task, group_map=group_map, config=config)
        elif task in EVENT_MAP_TASKS:
            self._event_plan_active_task = task
            self._render_event_map_layout(task=task, group_map=group_map, config=config)
        elif task == "EventShop":
            self._event_plan_active_task = task
            self._render_event_shop_layout(task=task, group_map=group_map, config=config)

    def alas_set_group(self, task: str) -> None:
        if task not in EVENT_LAYOUT_TASKS:
            self._unmark_event_page()
            return super().alas_set_group(task)
        return self._alas_set_event_group(task)
