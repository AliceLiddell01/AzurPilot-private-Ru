"""Provider-neutral Event planning UI.

This layer owns only user-facing planning state. Existing Scheduler, Campaign and
EventShop runtime contracts stay authoritative and are updated explicitly when the
user presses an apply button.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from functools import partial
from threading import RLock
from typing import Any, Dict, Mapping

from module.config.time_sentinel import is_default_time
from module.webui.app_dependencies import (
    close_popup,
    current_time,
    deep_get,
    load_event_calculator,
    logger,
    pin,
    popup,
    put_button,
    put_buttons,
    put_html,
    put_input,
    put_row,
    put_select,
    put_table,
    put_text,
    toast,
)
from module.webui.app_helpers import is_demo_mode
from module.webui.app_types import WebUIMixinBase
from module.webui.event_config import update_event_config
from module.webui.event_plan import (
    empty_event_plan,
    estimate_stage_runs,
    event_farm_summary,
    import_legacy_event_calculator,
    load_event_plan,
    save_event_plan,
    selected_shop_filter_tokens,
    selected_shop_items_missing_filter,
    shop_plan_total,
)


_EVENT_NAME_PIN = "event_plan_event_name"
_EVENT_FARM_END_PIN = "event_plan_farm_end"
_EVENT_SHOP_END_PIN = "event_plan_shop_end"
_EVENT_CURRENT_PT_PIN = "event_plan_current_pt"
_EVENT_PT_MODE_PIN = "event_plan_pt_mode"
_STAGE_NAME_PIN = "event_plan_stage_name"
_STAGE_PT_PIN = "event_plan_stage_pt"
_POINT_NAME_PIN = "event_plan_point_name"
_POINT_PT_PIN = "event_plan_point_pt"
_SHOP_NAME_PIN = "event_plan_shop_name"
_SHOP_PRICE_PIN = "event_plan_shop_price"
_SHOP_STOCK_PIN = "event_plan_shop_stock"
_SHOP_FILTER_PIN = "event_plan_shop_filter"
_SHOP_SELECTED_PIN = "event_plan_shop_selected"

_POINT_GROUP_LABELS = {
    "daily": "Ежедневные задания",
    "extra": "Дополнительно за день",
}

_EVENT_PLAN_MUTATION_LOCK = RLock()
_STALE_EVENT_PLAN = object()


class EventPlannerMixin(WebUIMixinBase):
    """Render and mutate the local Event plan without coupling it to a provider."""

    def _event_plan(self) -> Dict[str, Any]:
        return load_event_plan(self.alas_name)

    def _event_plan_write(self, plan: Mapping[str, Any], message: str) -> bool:
        if is_demo_mode():
            toast("В демонстрационном режиме изменение плана ивента отключено.", color="warning")
            return False
        try:
            with _EVENT_PLAN_MUTATION_LOCK:
                save_event_plan(self.alas_name, plan)
        except Exception as exc:
            logger.exception(exc)
            toast(f"Не удалось сохранить план ивента: {exc}", color="error")
            return False
        if message:
            toast(message, color="success")
        return True

    def _event_plan_mutate(self, mutation, message: str) -> bool:
        """Serialize one load-mutate-save cycle across concurrent WebUI sessions."""
        with _EVENT_PLAN_MUTATION_LOCK:
            plan = self._event_plan()
            if mutation(plan) is _STALE_EVENT_PLAN:
                self._stale_plan_message()
                return False
            return self._event_plan_write(plan, message)

    def _event_config_update(self, updates: Mapping[str, Any]) -> None:
        """Write Event runtime fields and surface failures to the caller."""
        update_event_config(self.alas_config, self.alas_name, updates)
        self.alas_config.load()

    @staticmethod
    def _event_plan_source_label(plan: Mapping[str, Any]) -> str:
        event = plan.get("event", {})
        source = event.get("source", {}) if isinstance(event, Mapping) else {}
        kind = source.get("kind") if isinstance(source, Mapping) else ""
        verified = bool(source.get("verified")) if isinstance(source, Mapping) else False
        if kind == "legacy_bwiki":
            return "Legacy BWiki — не подтверждено"
        if kind == "azurlane_lua":
            return "Игровые данные Azur Lane" + (" — подтверждено" if verified else "")
        if kind == "manual" and verified:
            return "Введено и подтверждено вручную"
        if kind == "manual":
            return "Локальный ручной план"
        return str(kind or "Локальный план")

    @staticmethod
    def _valid_datetime_text(value: str) -> bool:
        text = str(value or "").strip().replace("T", " ")
        if not text:
            return True
        if not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2}(?::\d{2})?)?",
            text,
        ):
            return False
        try:
            if len(text) == 10:
                date.fromisoformat(text)
            else:
                datetime.fromisoformat(text)
        except ValueError:
            return False
        return True

    @staticmethod
    def _config_datetime(value: str) -> str:
        value = value.strip().replace("T", " ")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            # A date-only limit means the whole selected day is available.
            return f"{value} 23:59:59"
        if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", value):
            return f"{value}:00"
        return value

    @staticmethod
    def _stale_plan_message() -> None:
        toast(
            "План ивента изменился после отрисовки страницы. Обновите страницу и повторите действие.",
            color="warning",
            duration=6,
        )

    @staticmethod
    def _find_exact_row(rows, identity):
        matches = [index for index, item in enumerate(rows) if identity(item)]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _shop_item_identity(item: Mapping[str, Any]) -> tuple[str, str, str, int, int]:
        return (
            str(item.get("id") or ""),
            str(item.get("name") or ""),
            str(item.get("filter") or ""),
            int(item.get("price", 0) or 0),
            int(item.get("stock", 0) or 0),
        )

    @classmethod
    def _find_shop_item(cls, items, identity):
        source_id, name, filter_token, price, stock = identity
        if source_id:
            matches = [
                index
                for index, item in enumerate(items)
                if str(item.get("id") or "") == source_id
            ]
        else:
            matches = [
                index
                for index, item in enumerate(items)
                if cls._shop_item_identity(item) == identity
            ]
        return matches[0] if len(matches) == 1 else None

    def _refresh_event_plan_page(self) -> None:
        task = getattr(self, "_event_plan_active_task", "")
        if task:
            self.alas_set_group(task)

    def _edit_event_metadata_popup(self) -> None:
        plan = self._event_plan()
        event = plan["event"]
        popup(
            "Данные и прогресс текущего ивента",
            [
                put_input(_EVENT_NAME_PIN, label="Название", value=event.get("name", "")),
                put_input(
                    _EVENT_FARM_END_PIN,
                    label="Окончание фарма",
                    value=event.get("farm_end", ""),
                    placeholder="YYYY-MM-DD HH:MM:SS",
                ),
                put_input(
                    _EVENT_SHOP_END_PIN,
                    label="Окончание магазина",
                    value=event.get("shop_end", ""),
                    placeholder="YYYY-MM-DD HH:MM:SS",
                ),
                put_select(
                    _EVENT_PT_MODE_PIN,
                    label="Источник текущего PT",
                    value=plan["progress"].get("pt_mode", "auto"),
                    options=[
                        {"label": "Автоматически из последнего OCR", "value": "auto"},
                        {"label": "Вручную", "value": "manual"},
                    ],
                ),
                put_input(
                    _EVENT_CURRENT_PT_PIN,
                    type="number",
                    label="Текущий PT — ручное значение / fallback",
                    min=0,
                    value=plan["progress"].get("current_pt", 0),
                    help_text="Используется в ручном режиме или пока AzurPilot ещё не записал PT через OCR.",
                ),
                put_row(
                    [
                        put_button("Сохранить", onclick=self._save_event_metadata_popup, color="primary"),
                        put_button(
                            "Сохранить и подтвердить даты",
                            onclick=partial(self._save_event_metadata_popup, True),
                            color="off",
                        ),
                        put_button("Отмена", onclick=close_popup, color="off"),
                    ],
                    size="auto auto auto",
                ),
            ],
        )

    def _save_event_metadata_popup(self, confirm_dates: bool = False) -> None:
        name = str(pin[_EVENT_NAME_PIN] or "").strip()
        farm_end = str(pin[_EVENT_FARM_END_PIN] or "").strip()
        shop_end = str(pin[_EVENT_SHOP_END_PIN] or "").strip()
        try:
            current_pt = int(pin[_EVENT_CURRENT_PT_PIN] or 0)
        except (TypeError, ValueError):
            current_pt = -1
        pt_mode = str(pin[_EVENT_PT_MODE_PIN] or "auto").lower()
        if pt_mode not in {"auto", "manual"}:
            pt_mode = "auto"
        if current_pt < 0:
            toast("Текущий PT не может быть отрицательным", color="warning")
            return
        if not self._valid_datetime_text(farm_end) or not self._valid_datetime_text(shop_end):
            toast("Дата должна быть в формате YYYY-MM-DD или YYYY-MM-DD HH:MM:SS", color="warning")
            return

        def mutation(plan):
            event = plan["event"]
            date_changed = (
                farm_end != str(event.get("farm_end") or "")
                or shop_end != str(event.get("shop_end") or "")
            )
            event.update({"name": name, "farm_end": farm_end, "shop_end": shop_end})
            # Do not launder imported/unverified dates merely because the user renamed
            # the event. Dates become trusted only after explicit manual editing or an
            # explicit confirmation action.
            if date_changed or confirm_dates:
                event["source"] = {
                    "kind": "manual",
                    "verified": True,
                    "updated_at": "",
                    "revision": "",
                }
            plan["progress"].update({"current_pt": current_pt, "pt_mode": pt_mode})

        if self._event_plan_mutate(mutation, "Данные и прогресс ивента сохранены"):
            close_popup()
            self._refresh_event_plan_page()

    def _add_stage_popup(self) -> None:
        popup(
            "Добавить этап",
            [
                put_input(_STAGE_NAME_PIN, label="Название этапа", placeholder="Например: HT3"),
                put_input(_STAGE_PT_PIN, type="number", label="PT за прохождение", min=1, value=1),
                put_row(
                    [
                        put_button("Добавить", onclick=self._save_stage_popup, color="primary"),
                        put_button("Отмена", onclick=close_popup, color="off"),
                    ],
                    size="auto auto",
                ),
            ],
        )

    def _save_stage_popup(self) -> None:
        name = str(pin[_STAGE_NAME_PIN] or "").strip().upper()
        try:
            points = int(pin[_STAGE_PT_PIN] or 0)
        except (TypeError, ValueError):
            points = 0
        if not name or points <= 0:
            toast("Укажите название этапа и положительное количество PT", color="warning")
            return
        def mutation(plan):
            stages = [item for item in plan["stages"] if item["name"].upper() != name]
            stages.append({"name": name, "points": points})
            plan["stages"] = stages

        if self._event_plan_mutate(mutation, f"Этап {name} добавлен в план"):
            close_popup()
            self._refresh_event_plan_page()

    def _delete_stage(self, name: str, points: int) -> None:
        def mutation(plan):
            index = self._find_exact_row(
                plan["stages"],
                lambda item: item.get("name") == name
                and int(item.get("points", 0) or 0) == points,
            )
            if index is None:
                return _STALE_EVENT_PLAN
            del plan["stages"][index]

        if self._event_plan_mutate(mutation, f"Этап {name} удалён из плана"):
            self._refresh_event_plan_page()

    def _add_point_source_popup(self, kind: str) -> None:
        if kind not in _POINT_GROUP_LABELS:
            return
        popup(
            f"Добавить: {_POINT_GROUP_LABELS[kind]}",
            [
                put_input(_POINT_NAME_PIN, label="Название"),
                put_input(_POINT_PT_PIN, type="number", label="PT за день", min=1, value=1),
                put_row(
                    [
                        put_button(
                            "Добавить",
                            onclick=partial(self._save_point_source_popup, kind),
                            color="primary",
                        ),
                        put_button("Отмена", onclick=close_popup, color="off"),
                    ],
                    size="auto auto",
                ),
            ],
        )

    def _save_point_source_popup(self, kind: str) -> None:
        if kind not in _POINT_GROUP_LABELS:
            close_popup()
            return
        name = str(pin[_POINT_NAME_PIN] or "").strip()
        try:
            points = int(pin[_POINT_PT_PIN] or 0)
        except (TypeError, ValueError):
            points = 0
        if not name or points <= 0:
            toast("Укажите название и положительное количество PT", color="warning")
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

    def _point_source_action(self, kind: str, name: str, points: int, action: str) -> None:
        if kind not in _POINT_GROUP_LABELS:
            return
        message = {
            "skip": "Статус пропуска источника обновлён",
            "done": "Статус получения за сегодня обновлён",
            "delete": f"Источник «{name}» удалён",
        }.get(action)
        if message is None:
            return

        def mutation(plan):
            index = self._find_exact_row(
                plan[kind],
                lambda item: item.get("name") == name
                and int(item.get("points", 0) or 0) == points,
            )
            if index is None:
                return _STALE_EVENT_PLAN
            item = plan[kind][index]
            if action == "skip":
                item["skip"] = not bool(item.get("skip"))
            elif action == "done":
                today = current_time().date().isoformat()
                item["completed_date"] = "" if item.get("completed_date") == today else today
            else:
                del plan[kind][index]

        if self._event_plan_mutate(mutation, message):
            self._refresh_event_plan_page()

    def _render_point_sources(self, plan: Mapping[str, Any], kind: str) -> None:
        rows = plan.get(kind, [])
        title = _POINT_GROUP_LABELS[kind]
        put_text(title).style("font-weight: 600; margin-top: .8rem;")
        if rows:
            today = current_time().date().isoformat()
            table = []
            for item in rows:
                table.append(
                    [
                        item["name"],
                        item["points"],
                        "Да" if item.get("skip") else "Нет",
                        "Да" if item.get("completed_date") == today else "Нет",
                        put_buttons(
                            [
                                {"label": "Пропуск", "value": "skip", "color": "off"},
                                {"label": "Получено сегодня", "value": "done", "color": "off"},
                                {"label": "Удалить", "value": "delete", "color": "off"},
                            ],
                            onclick=partial(self._point_source_action, kind, item["name"], item["points"]),
                        ),
                    ]
                )
            put_table(table, header=["Источник", "PT/день", "Пропускать", "Получено сегодня", ""])
        else:
            put_text("Нет источников для расчёта.").style("opacity: .72;")
        put_button(
            f"Добавить — {title.lower()}",
            onclick=partial(self._add_point_source_popup, kind),
            color="off",
        )

    def _import_legacy_bwiki_cache(self) -> None:
        data = load_event_calculator(force_refresh=False)
        if data.get("error") and not data.get("shop_items"):
            toast(
                "Локального кэша BWiki нет. При необходимости откройте legacy-блок и явно обновите его.",
                color="warning",
                duration=6,
            )
            return
        def mutation(plan):
            imported = import_legacy_event_calculator(data, server="EN")
            imported["progress"] = plan["progress"]
            plan.clear()
            plan.update(imported)

        if self._event_plan_mutate(
            mutation,
            "Legacy BWiki импортирован в локальный план. Данные помечены как неподтверждённые.",
        ):
            self._refresh_event_plan_page()

    def _clear_event_plan(self) -> None:
        def mutation(plan):
            plan.clear()
            plan.update(empty_event_plan("EN"))

        if self._event_plan_mutate(mutation, "Локальный план ивента очищен"):
            self._refresh_event_plan_page()

    def _apply_farm_end(self) -> None:
        plan = self._event_plan()
        event = plan["event"]
        farm_end = str(event.get("farm_end") or "").strip()
        source = event.get("source", {})
        if not farm_end:
            toast("В плане не задано время окончания фарма", color="warning")
            return
        if not isinstance(source, Mapping) or not bool(source.get("verified")):
            toast(
                "Дата не подтверждена. Сначала откройте данные ивента и подтвердите её вручную.",
                color="warning",
                duration=6,
            )
            return
        value = self._config_datetime(farm_end)
        try:
            self._event_config_update({"EventGeneral.EventGeneral.TimeLimit": value})
        except Exception as exc:
            logger.exception(exc)
            toast(f"Не удалось записать окончание фарма: {exc}", color="error")
            return
        toast("Окончание фарма записано в автостоп", color="success")
        self._refresh_event_plan_page()

    def _use_shop_total_as_target(self) -> None:
        total = shop_plan_total(self._event_plan())
        if total <= 0:
            toast("В плане магазина пока нет выбранных товаров", color="warning")
            return
        try:
            self._event_config_update({"EventGeneral.EventGeneral.PtLimit": total})
        except Exception as exc:
            logger.exception(exc)
            toast(f"Не удалось записать целевой PT: {exc}", color="error")
            return
        toast(f"Целевой PT установлен по плану магазина: {total}", color="success")
        self._refresh_event_plan_page()

    def _add_shop_item_popup(self) -> None:
        popup(
            "Добавить товар в план",
            [
                put_input(_SHOP_NAME_PIN, label="Название товара"),
                put_input(_SHOP_PRICE_PIN, type="number", label="Цена", min=1, value=1),
                put_input(_SHOP_STOCK_PIN, type="number", label="Доступно в магазине", min=1, value=1),
                put_input(
                    _SHOP_FILTER_PIN,
                    label="Токен фильтра AzurPilot — необязательно",
                    placeholder="Например: Cube, Chip, ShipSSR",
                    help_text=(
                        "Нужен только для автоматической покупки через EventShop DSL. "
                        "Количество берётся из плана автоматически — суффикс :N вводить не нужно."
                    ),
                ),
                put_row(
                    [
                        put_button("Добавить", onclick=self._save_shop_item_popup, color="primary"),
                        put_button("Отмена", onclick=close_popup, color="off"),
                    ],
                    size="auto auto",
                ),
            ],
        )

    def _save_shop_item_popup(self) -> None:
        name = str(pin[_SHOP_NAME_PIN] or "").strip()
        filter_token = str(pin[_SHOP_FILTER_PIN] or "").strip()
        try:
            price = int(pin[_SHOP_PRICE_PIN] or 0)
            stock = int(pin[_SHOP_STOCK_PIN] or 0)
        except (TypeError, ValueError):
            price = stock = 0
        if not name or price <= 0 or stock <= 0:
            toast("Укажите название, положительную цену и количество", color="warning")
            return
        def mutation(plan):
            plan["shop_items"].append(
                {
                    "name": name,
                    "price": price,
                    "stock": stock,
                    "selected": stock,
                    "filter": filter_token,
                }
            )

        if self._event_plan_mutate(mutation, f"Товар «{name}» добавлен в план"):
            close_popup()
            self._refresh_event_plan_page()

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

    def _save_shop_quantity_popup(self, identity: tuple[str, str, str, int, int]) -> None:
        try:
            selected = int(pin[_SHOP_SELECTED_PIN] or 0)
        except (TypeError, ValueError):
            selected = -1

        max_stock = identity[4]
        if selected < 0 or selected > max_stock:
            toast(f"Количество должно быть от 0 до {max_stock}", color="warning")
            return

        def mutation(plan):
            index = self._find_shop_item(plan["shop_items"], identity)
            if index is None:
                return _STALE_EVENT_PLAN
            item = plan["shop_items"][index]
            item["selected"] = selected

        if self._event_plan_mutate(mutation, "Количество в плане обновлено"):
            close_popup()
            self._refresh_event_plan_page()

    def _delete_shop_item(self, identity: tuple[str, str, str, int, int]) -> None:
        name = identity[1]

        def mutation(plan):
            index = self._find_shop_item(plan["shop_items"], identity)
            if index is None:
                return _STALE_EVENT_PLAN
            del plan["shop_items"][index]

        if self._event_plan_mutate(mutation, f"Товар «{name}» удалён из плана"):
            self._refresh_event_plan_page()

    def _set_all_shop_quantities(self, selected_all: bool) -> None:
        def mutation(plan):
            for item in plan["shop_items"]:
                item["selected"] = item["stock"] if selected_all else 0

        message = "Все товары выбраны" if selected_all else "План покупок очищен"
        if self._event_plan_mutate(mutation, message):
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
                "Для выбранных товаров нет безопасного токена фильтра AzurPilot: " + ", ".join(missing),
                color="warning",
                duration=8,
            )
            return

        tokens = selected_shop_filter_tokens(plan)
        if not tokens:
            toast("Не удалось построить фильтр магазина", color="warning")
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
        toast("План синхронизирован с целевым PT и фильтром EventShop", color="success")
        self._refresh_event_plan_page()

    @staticmethod
    def _current_pt_for_plan(config: Mapping[str, Any], plan: Mapping[str, Any]) -> tuple[int, str]:
        progress = plan.get("progress", {})
        mode = str(progress.get("pt_mode") or "auto").lower() if isinstance(progress, Mapping) else "auto"
        manual_pt = int(progress.get("current_pt", 0) or 0) if isinstance(progress, Mapping) else 0
        dashboard_pt = int(deep_get(config, "Dashboard.Pt.Value", 0) or 0)
        dashboard_record = str(deep_get(config, "Dashboard.Pt.Record", "") or "").strip()
        dashboard_time = None
        if dashboard_record:
            try:
                dashboard_time = datetime.fromisoformat(dashboard_record.replace("T", " "))
            except ValueError:
                dashboard_time = None
        now = current_time().replace(tzinfo=None, microsecond=0)
        dashboard_valid = bool(
            dashboard_time is not None
            and not is_default_time(dashboard_time)
            and timedelta(0) <= now - dashboard_time <= timedelta(hours=48)
        )
        if mode == "auto" and dashboard_valid:
            return dashboard_pt, f"Автоматически из OCR ({dashboard_record})"
        if mode == "manual":
            return manual_pt, "Вручную"
        if dashboard_time is not None and not is_default_time(dashboard_time):
            return manual_pt, "Ручной fallback — OCR PT устарел"
        return manual_pt, "Ручной fallback — OCR PT ещё не записан"

    def _render_event_plan_general(self, config: Mapping[str, Any]) -> None:
        self._event_plan_active_task = "EventGeneral"
        plan = self._event_plan()
        event = plan["event"]
        target = int(deep_get(config, "EventGeneral.EventGeneral.PtLimit", 0) or 0)
        shop_total = shop_plan_total(plan)
        effective_target = target if target > 0 else shop_total
        current_pt, current_pt_source = self._current_pt_for_plan(config, plan)
        now = current_time().replace(tzinfo=None, microsecond=0)
        forecast = event_farm_summary(plan, effective_target, current_pt=current_pt, today=now)
        source = event.get("source", {})

        put_text("План ивента")
        put_text(
            "Локальная модель не зависит от BWiki. Текущий PT, даты, этапы и ежедневные источники можно вести вручную; внешний источник позже сможет заполнить тот же план без переделки страницы."
        ).style("font-size: .9rem; opacity: .78;")
        put_html('<hr class="hr-group">')

        summary = [
            ["Текущий ивент", event.get("name") or "Не задан"],
            ["Сервер", event.get("server") or "EN"],
            ["Окончание фарма", event.get("farm_end") or "Не задано"],
            ["Окончание магазина", event.get("shop_end") or "Не задано"],
            ["Источник", self._event_plan_source_label(plan)],
            ["Текущий PT", forecast["current_pt"]],
            ["Источник текущего PT", current_pt_source],
            ["Целевой PT", effective_target or "Без ограничения"],
            ["Стоимость выбранных покупок", shop_total],
            ["Дней фарма в расчёте", forecast["remaining_days"] if event.get("farm_end") else "Не задано"],
            ["Ожидается из ежедневных источников", forecast["recurring_pt"]],
            ["Осталось нафармить картами", forecast["farm_required_pt"]],
        ]
        revision = str(source.get("revision") or "") if isinstance(source, Mapping) else ""
        updated_at = str(source.get("updated_at") or "") if isinstance(source, Mapping) else ""
        if revision:
            summary.append(["Ревизия источника", revision])
        if updated_at:
            summary.append(["Данные источника обновлены", updated_at])
        put_table(summary, header=["Параметр", "Значение"])

        put_row(
            [
                put_button("Данные и прогресс ивента", onclick=self._edit_event_metadata_popup, color="primary"),
                put_button("Взять цель из магазина", onclick=self._use_shop_total_as_target, color="off"),
                put_button("Записать окончание фарма", onclick=self._apply_farm_end, color="off"),
            ],
            size="auto auto auto",
        )

        self._render_point_sources(plan, "daily")
        self._render_point_sources(plan, "extra")

        stage_rows = []
        for stage in estimate_stage_runs(plan, forecast["farm_required_pt"]):
            stage_rows.append(
                [
                    stage["name"],
                    stage["points"],
                    stage["runs"],
                    put_button(
                        "Удалить",
                        onclick=partial(self._delete_stage, stage["name"], stage["points"]),
                        color="off",
                    ),
                ]
            )
        if stage_rows:
            put_text("Расчёт фарма по этапам").style("font-weight: 600; margin-top: .8rem;")
            put_table(stage_rows, header=["Этап", "PT", "Нужно проходов", ""])
        else:
            put_text("Этапы для расчёта пока не добавлены.").style("opacity: .72; margin-top: .8rem;")

        put_row(
            [
                put_button("Добавить этап", onclick=self._add_stage_popup, color="off"),
                put_button("Импортировать локальный legacy BWiki", onclick=self._import_legacy_bwiki_cache, color="off"),
                put_button("Очистить локальный план", onclick=self._clear_event_plan, color="off"),
            ],
            size="auto auto auto",
        )

    def _render_event_shop_plan(self, config: Mapping[str, Any]) -> None:
        self._event_plan_active_task = "EventShop"
        plan = self._event_plan()
        items = plan["shop_items"]
        total = shop_plan_total(plan)
        event_name = plan["event"].get("name") or "Текущий ивент не задан"

        put_text("План покупок")
        put_text(f"{event_name}. Выберите нужное количество товаров — итог используется как целевой PT.").style(
            "font-size: .9rem; opacity: .78;"
        )
        put_html('<hr class="hr-group">')

        if items:
            rows = []
            for item in items:
                identity = self._shop_item_identity(item)
                rows.append(
                    [
                        item["name"],
                        item["price"],
                        item["stock"],
                        item["selected"],
                        item["price"] * item["selected"],
                        put_buttons(
                            [
                                {"label": "Количество", "value": "edit", "color": "off"},
                                {"label": "Удалить", "value": "delete", "color": "off"},
                            ],
                            onclick=lambda action, key=identity: (
                                self._shop_quantity_popup(key)
                                if action == "edit"
                                else self._delete_shop_item(key)
                            ),
                        ),
                    ]
                )
            put_table(rows, header=["Товар", "Цена", "Доступно", "Купить", "Итого", ""])
            put_text(f"Стоимость выбранного плана: {total} PT").style(
                "font-size: 1.05rem; font-weight: 600; margin-top: .7rem;"
            )
        else:
            put_text(
                "План магазина пуст. Добавьте товары вручную или импортируйте существующий локальный legacy-кэш."
            ).style("opacity: .78;")

        put_row(
            [
                put_button("Добавить товар", onclick=self._add_shop_item_popup, color="primary"),
                put_button("Выбрать всё", onclick=partial(self._set_all_shop_quantities, True), color="off"),
                put_button("Очистить выбор", onclick=partial(self._set_all_shop_quantities, False), color="off"),
                put_button("Импортировать legacy BWiki", onclick=self._import_legacy_bwiki_cache, color="off"),
            ],
            size="auto auto auto auto",
        )
        put_row(
            [
                put_button("Только записать целевой PT", onclick=self._use_shop_total_as_target, color="off"),
                put_button(
                    "Синхронизировать с EventShop",
                    onclick=self._apply_shop_plan_to_automation,
                    color="primary",
                ),
            ],
            size="auto auto",
        )

        missing = selected_shop_items_missing_filter(plan)
        if missing:
            put_text(
                "Некоторым выбранным товарам не назначен внутренний токен автоматизации. "
                "Целевой PT считать можно, но синхронизация с EventShop для них заблокирована: "
                + ", ".join(missing)
            ).style("font-size: .85rem; opacity: .78;")
