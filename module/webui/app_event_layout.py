"""Presentation-only layout for the cleaned Event pages.

This module deliberately changes only the WebUI composition. Runtime task IDs,
argument schemas and campaign behaviour remain untouched.
"""

from __future__ import annotations

import json
from datetime import datetime
from html import escape
from typing import Any, Dict, Iterable, Tuple

from module.config.time_sentinel import DEFAULT_TIME_TEXT, is_default_time
from module.webui.app_dependencies import (
    close_popup,
    deep_get,
    deep_iter,
    load_event_calculator,
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
    put_text,
    run_js,
    t,
    toast,
    use_scope,
)
from module.webui.app_event_planner import EventPlannerMixin
from module.webui.app_helpers import is_demo_mode
from module.webui.event_plan import selected_shop_filter_conflicts


EVENT_MAP_TASKS = frozenset({"Event", "Event2", "Event3"})
EVENT_LAYOUT_TASKS = EVENT_MAP_TASKS | {"EventGeneral", "EventShop"}

EVENT_MAP_PRIMARY_GROUPS = (
    "Scheduler",
    "Campaign",
    "StopCondition",
    "Fleet",
    "Emotion",
)
EVENT_MAP_ADVANCED_GROUPS = (
    "Submarine",
    "HpControl",
    "EnemyPriority",
)

_EVENT_TARGET_PIN = "event_stop_target_pt"
_DISABLED_EVENT_TIME = DEFAULT_TIME_TEXT


class EventLayoutMixin(EventPlannerMixin):
    """Compose Event pages without changing their underlying config contracts."""

    @staticmethod
    def _event_group_map(task_args: Dict[str, Any]) -> Dict[str, Tuple[Any, Any]]:
        return {
            group[0]: (group, arg_dict)
            for group, arg_dict in deep_iter(task_args, depth=1)
        }

    def _render_event_named_group(
        self,
        *,
        task: str,
        name: str,
        group_map: Dict[str, Tuple[Any, Any]],
        config: Dict[str, Any],
        navigator: bool = True,
    ) -> int:
        item = group_map.get(name)
        if item is None:
            return 0
        group, arg_dict = item
        rendered = self.set_group(group, arg_dict, config, task)
        if rendered and navigator:
            self.set_navigator(group)
        return rendered

    def _render_event_collapsed_groups(
        self,
        *,
        task: str,
        title: str,
        description: str,
        names: Iterable[str],
        group_map: Dict[str, Tuple[Any, Any]],
        config: Dict[str, Any],
    ) -> None:
        """Render generic groups once, then move their scopes into native details.

        `TaskConfigMixin.set_group()` owns creation of `group_<name>` scopes. The
        previous implementation pre-created the same scopes inside `put_collapse()`
        and then called `set_group()`, which caused PyWebIO's duplicate-scope error.
        """
        existing = [name for name in names if name in group_map]
        if not existing:
            return

        suffix = "-".join(name.lower() for name in existing)
        details_id = f"event-advanced-{task.lower()}-{suffix}"
        body_id = f"{details_id}-body"
        with use_scope("groups"):
            put_html(
                f'<details id="{escape(details_id)}" class="event-advanced-details">'
                f'<summary>{escape(title)}</summary>'
                f'<div class="event-advanced-description">{escape(description)}</div>'
                f'<div id="{escape(body_id)}" class="event-advanced-body"></div>'
                '</details>'
            )

        rendered_names = []
        for name in existing:
            if self._render_event_named_group(
                task=task,
                name=name,
                group_map=group_map,
                config=config,
                navigator=False,
            ):
                rendered_names.append(name)

        if not rendered_names:
            return
        scope_ids = [f"pywebio-scope-group_{name}" for name in rendered_names]
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
""" % (json.dumps(body_id), json.dumps(scope_ids))
        )

    @staticmethod
    def _event_time_limit_label(value: Any) -> str:
        if is_default_time(value):
            return "Без ограничения"
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d %H:%M:%S")
        text = str(value or "").strip()
        return text or "Без ограничения"

    @staticmethod
    def _event_write_allowed() -> bool:
        if not is_demo_mode():
            return True
        toast("В демонстрационном режиме изменение настроек ивента отключено.", color="warning")
        return False

    def _edit_event_target_popup(self) -> None:
        config = self.alas_config.read_file(self.alas_name)
        target = int(deep_get(config, "EventGeneral.EventGeneral.PtLimit", 0) or 0)
        popup(
            "Целевой PT",
            [
                put_input(
                    _EVENT_TARGET_PIN,
                    type="number",
                    label="Остановить фарм после достижения PT",
                    value=target,
                    min=0,
                    help_text="0 — без ограничения по PT.",
                ),
                put_row(
                    [
                        put_button("Сохранить", onclick=self._save_event_target_popup, color="primary"),
                        put_button("Отмена", onclick=close_popup, color="off"),
                    ],
                    size="auto auto",
                ),
            ],
        )

    def _save_event_target_popup(self) -> None:
        if not self._event_write_allowed():
            return
        try:
            target = int(pin[_EVENT_TARGET_PIN] or 0)
        except (TypeError, ValueError):
            target = -1
        if target < 0:
            toast("Целевой PT не может быть отрицательным", color="warning")
            return
        self._save_config(
            {"EventGeneral.EventGeneral.PtLimit": target},
            self.alas_name,
            self.alas_config,
        )
        self.alas_config.load()
        close_popup()
        toast("Целевой PT обновлён", color="success")
        self._refresh_event_plan_page()

    def _disable_event_time_limit(self) -> None:
        if not self._event_write_allowed():
            return
        self._save_config(
            {"EventGeneral.EventGeneral.TimeLimit": _DISABLED_EVENT_TIME},
            self.alas_name,
            self.alas_config,
        )
        self.alas_config.load()
        toast("Ограничение по времени отключено", color="success")
        self._refresh_event_plan_page()

    def _use_shop_total_as_target(self) -> None:
        if not self._event_write_allowed():
            return
        return super()._use_shop_total_as_target()

    def _apply_farm_end(self) -> None:
        if not self._event_write_allowed():
            return
        return super()._apply_farm_end()

    def _render_event_stop_controls(self, config: Dict[str, Any]) -> None:
        target = int(deep_get(config, "EventGeneral.EventGeneral.PtLimit", 0) or 0)
        time_limit = deep_get(config, "EventGeneral.EventGeneral.TimeLimit", _DISABLED_EVENT_TIME)

        put_text("Цель и автостоп ивента")
        put_text(
            "Эти значения управляют остановкой всех задач текущего ивента. Служебные значения отключения скрыты и показываются как «Без ограничения»."
        ).style("font-size: .9rem; opacity: .78;")
        put_html('<hr class="hr-group">')
        put_table(
            [
                ["Целевой PT", target if target > 0 else "Без ограничения"],
                ["Остановить по времени", self._event_time_limit_label(time_limit)],
            ],
            header=["Параметр", "Значение"],
        )
        put_row(
            [
                put_button("Изменить целевой PT", onclick=self._edit_event_target_popup, color="primary"),
                put_button(
                    "Взять PT из плана магазина",
                    onclick=self._use_shop_total_as_target,
                    color="off",
                ),
                put_button(
                    "Записать окончание фарма из плана",
                    onclick=self._apply_farm_end,
                    color="off",
                ),
                put_button(
                    "Отключить ограничение по времени",
                    onclick=self._disable_event_time_limit,
                    color="off",
                ),
            ],
            size="auto auto auto auto",
        )

    def _refresh_legacy_bwiki_cache(self) -> None:
        """Refresh the legacy cache only after an explicit user action."""
        if not self._event_write_allowed():
            return
        data = load_event_calculator(force_refresh=True)
        error = data.get("error")
        if error:
            if data.get("from_cache") and data.get("shop_items"):
                toast(
                    "BWiki сейчас недоступна или вернула ошибку; оставлен предыдущий локальный кэш. "
                    f"Причина: {error}",
                    color="warning",
                    duration=8,
                )
                return
            toast(f"Не удалось обновить legacy BWiki: {error}", color="error", duration=8)
            return
        event_name = str(data.get("event_name") or "данные ивента")
        toast(
            f"Legacy-кэш BWiki обновлён: {event_name}. Перед применением импортируйте его в локальный план.",
            color="success",
            duration=6,
        )

    def _render_event_general_layout(
        self,
        *,
        task: str,
        group_map: Dict[str, Tuple[Any, Any]],
        config: Dict[str, Any],
    ) -> None:
        with use_scope("groups"):
            put_scope("group_EventStop")
        with use_scope("group_EventStop", clear=True):
            self._render_event_stop_controls(config)

        with use_scope("groups"):
            put_scope("group_EventPlan")
        with use_scope("group_EventPlan", clear=True):
            self._render_event_plan_general(config)

        self._render_event_collapsed_groups(
            task=task,
            title="Расширенные настройки — баланс задач",
            description=(
                "Автоматический баланс монет и переключение на другую задачу. "
                "Для обычного фарма ивента эти параметры не требуются."
            ),
            names=("TaskBalancer",),
            group_map=group_map,
            config=config,
        )

        with use_scope("groups"):
            put_collapse(
                title="Резервный источник — BWiki (legacy)",
                content=[
                    put_text(
                        "BWiki больше не используется как основной калькулятор и никогда не записывает данные "
                        "в настройки автоматически. Кэш можно обновить вручную, после чего импортировать в "
                        "локальный план как неподтверждённый источник. Для EN данные BWiki могут отставать."
                    ).style("font-size: .9rem; opacity: .82; margin-bottom: .5rem;"),
                    put_row(
                        [
                            put_button(
                                "Обновить legacy-кэш BWiki",
                                onclick=self._refresh_legacy_bwiki_cache,
                                color="off",
                            ),
                            put_button(
                                "Импортировать кэш в локальный план",
                                onclick=self._import_legacy_bwiki_cache,
                                color="off",
                            ),
                        ],
                        size="auto auto",
                    ),
                ],
                open=False,
            )

    def _render_event_map_layout(
        self,
        *,
        task: str,
        group_map: Dict[str, Tuple[Any, Any]],
        config: Dict[str, Any],
    ) -> None:
        for name in EVENT_MAP_PRIMARY_GROUPS:
            self._render_event_named_group(
                task=task,
                name=name,
                group_map=group_map,
                config=config,
            )

        self._render_event_collapsed_groups(
            task=task,
            title="Расширенные настройки карты",
            description=(
                "Подводный флот, контроль HP и приоритет вражеских флотов. "
                "Обычный ивентовый фарм можно настроить без этих параметров."
            ),
            names=EVENT_MAP_ADVANCED_GROUPS,
            group_map=group_map,
            config=config,
        )

    def _render_event_shop_layout(
        self,
        *,
        task: str,
        group_map: Dict[str, Tuple[Any, Any]],
        config: Dict[str, Any],
    ) -> None:
        self._render_event_named_group(
            task=task,
            name="Scheduler",
            group_map=group_map,
            config=config,
        )

        with use_scope("groups"):
            put_scope("group_EventShopPlan")
        with use_scope("group_EventShopPlan", clear=True):
            self._render_event_shop_plan(config)

        self._render_event_collapsed_groups(
            task=task,
            title="Расширенные настройки — автоматизация магазина",
            description=(
                "Штатные параметры EventShop и ручной DSL-фильтр. "
                "Они сохранены полностью и используются runtime без изменения контракта."
            ),
            names=("EventShop",),
            group_map=group_map,
            config=config,
        )

    def _apply_shop_plan_to_automation(self) -> None:
        """Reject unsafe visual-to-DSL translations before touching runtime config."""
        if not self._event_write_allowed():
            return
        conflicts = selected_shop_filter_conflicts(self._event_plan())
        if conflicts:
            details = "; ".join(
                f"{token}: {', '.join(names)}" for token, names in conflicts.items()
            )
            toast(
                "Нельзя безопасно синхронизировать план: один токен фильтра относится и к выбранным, "
                f"и к исключённым товарам. {details}",
                color="warning",
                duration=9,
            )
            return
        return super()._apply_shop_plan_to_automation()

    @use_scope("content", clear=True)
    def _alas_set_event_group(self, task: str) -> None:
        config = self.alas_config.read_file(self.alas_name)
        self.init_menu(name=task)
        self.set_title(t(f"Task.{task}.name"))
        put_scope("_groups", [put_none(), put_scope("groups"), put_scope("navigator")])

        task_help: str = t(f"Task.{task}.help")
        if task_help:
            put_scope(
                "group__info",
                scope="groups",
                content=[put_text(task_help).style("font-size: 1rem")],
            )

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
        """Render Event pages with progressive disclosure; delegate all other tasks unchanged."""
        if task not in EVENT_LAYOUT_TASKS:
            return super().alas_set_group(task)
        return self._alas_set_event_group(task)
