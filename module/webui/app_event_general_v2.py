"""Компактный EventGeneral WebUI с обзором события и отдельной страницей наград."""

from __future__ import annotations

import re
from collections.abc import Mapping
from functools import partial
from html import escape
from typing import Any

from module.webui.app_dependencies import (
    logger,
    put_button,
    put_buttons,
    put_html,
    put_none,
    put_row,
    put_scope,
    run_js,
    toast,
    use_scope,
)
from module.webui.app_helpers import is_demo_mode
from module.webui.app_types import WebUIMixinBase
from module.webui.event_assets import event_asset_url
from module.webui.event_profiles import (
    EVENT_TASK_LABELS,
    OPTIONAL_EVENT_PROFILE_SLOTS,
    get_event_profile_metadata,
    next_available_event_profile_slot,
)


EVENT_REWARDS_TASK = "EventRewards"

_MAP_GROUPS = (
    ("A", "Карта A", "Нормальная сложность"),
    ("B", "Карта B", "Нормальная сложность"),
    ("C", "Карта C", "Hard-сложность"),
    ("D", "Карта D", "Hard-сложность"),
    ("SPECIAL", "Особые этапы", "SP и EXTRA"),
)
_QUEST_SOURCE_KINDS = frozenset({"unknown", "daily", "weekly", "one_time"})
_QUEST_DAILY_KINDS = frozenset({"daily"})


class EventGeneralV2Mixin(WebUIMixinBase):
    """Общие helpers, профильный UI и отдельная страница наград события."""

    def _ensure_event_rewards_menu_entry(self) -> None:
        """Добавить WebUI-only страницу наград в меню Event текущей сессии."""
        menus = getattr(self, "ALAS_MENU", None)
        if not isinstance(menus, Mapping):
            return
        event = menus.get("Event")
        if not isinstance(event, Mapping):
            return
        tasks = list(event.get("tasks") or [])
        if EVENT_REWARDS_TASK in tasks:
            return
        if "EventShop" in tasks:
            insert_at = tasks.index("EventShop") + 1
        elif "EventGeneral" in tasks:
            insert_at = tasks.index("EventGeneral") + 1
        else:
            insert_at = len(tasks)
        tasks.insert(insert_at, EVENT_REWARDS_TASK)
        copied_menus = dict(menus)
        copied_event = dict(event)
        copied_event["tasks"] = tasks
        copied_menus["Event"] = copied_event
        self.ALAS_MENU = copied_menus

    def alas_set_menu(self) -> None:
        self._ensure_event_rewards_menu_entry()
        return super().alas_set_menu()

    def alas_set_group(self, task: str) -> None:
        self._ensure_event_rewards_menu_entry()
        if task == "EventGeneral":
            return self._alas_set_event_general_v2()
        if task == EVENT_REWARDS_TASK:
            return self._alas_set_event_rewards_v2()
        return super().alas_set_group(task)

    def _refresh_event_profile_ui(self) -> None:
        """Сохранить CRUD профилей на компактной поверхности EventGeneral."""
        try:
            config = self._read_event_profile_config()
        except Exception as exc:
            logger.exception(exc)
            toast(f"Не удалось обновить интерфейс профилей: {exc}", color="error")
            return

        self._render_event_aware_menu()
        self.active_button("menu", "EventGeneral")
        with use_scope("group_EventProfiles", clear=True):
            self._render_event_profiles_compact(config)

    @staticmethod
    def _event_progress(plan: Mapping[str, Any]) -> tuple[int | None, Mapping[str, Any]]:
        progress = plan.get("progress", {})
        if not isinstance(progress, Mapping):
            progress = {}
        current = progress.get("current_pt")
        return (current if isinstance(current, int) else None), progress

    @staticmethod
    def _split_event_sources(
        plan: Mapping[str, Any],
    ) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
        sources = [
            item for item in plan.get("pt_sources", []) if isinstance(item, Mapping)
        ]
        quests = [item for item in sources if item.get("kind") in _QUEST_SOURCE_KINDS]
        overview = [item for item in sources if item.get("kind") not in _QUEST_SOURCE_KINDS]
        return overview, quests

    @staticmethod
    def _map_group_key(name: Any) -> str:
        value = str(name or "").strip().upper()
        if re.fullmatch(r"A\d+", value):
            return "A"
        if re.fullmatch(r"B\d+", value):
            return "B"
        if re.fullmatch(r"C\d+", value):
            return "C"
        if re.fullmatch(r"D\d+", value):
            return "D"
        if value == "SP" or value.startswith("EXTRA"):
            return "SPECIAL"
        return "OTHER"

    @classmethod
    def _group_map_items(
        cls, items: list[Mapping[str, Any]]
    ) -> list[tuple[str, str, list[Mapping[str, Any]]]]:
        grouped: dict[str, list[Mapping[str, Any]]] = {}
        for item in items:
            key = cls._map_group_key(item.get("name"))
            grouped.setdefault(key, []).append(item)

        result: list[tuple[str, str, list[Mapping[str, Any]]]] = []
        for key, title, subtitle in _MAP_GROUPS:
            rows = grouped.pop(key, [])
            if rows:
                result.append((title, subtitle, rows))
        other = grouped.pop("OTHER", [])
        for rows in grouped.values():
            other.extend(rows)
        if other:
            result.append(("Другие источники", "", other))
        return result

    @staticmethod
    def _ru_plural(number: int, one: str, few: str, many: str) -> str:
        value = abs(number)
        if value % 100 in range(11, 15):
            return many
        if value % 10 == 1:
            return one
        if value % 10 in range(2, 5):
            return few
        return many

    @classmethod
    def _quest_presentation(
        cls, item: Mapping[str, Any]
    ) -> tuple[str, str, str, str]:
        """Вернуть группу, русский заголовок, описание и исходный текст задания."""
        original = " ".join(str(item.get("name") or "").split()).strip()
        normalized = original.rstrip(".")
        kind = str(item.get("kind") or "")

        match = re.fullmatch(r"Build\s+(\d+)\s+ships?", normalized, re.IGNORECASE)
        if match:
            amount = int(match.group(1))
            noun = cls._ru_plural(amount, "корабль", "корабля", "кораблей")
            return (
                "daily",
                f"Построить {amount} {noun}",
                f"Постройте {amount} {noun} на верфи.",
                original,
            )

        match = re.fullmatch(
            r"Sortie\s+and\s+obtain\s+(\d+)\s+victories?",
            normalized,
            re.IGNORECASE,
        )
        if match:
            amount = int(match.group(1))
            noun = cls._ru_plural(amount, "победу", "победы", "побед")
            return (
                "daily",
                f"Одержать {amount} {noun}",
                f"Совершайте боевые выходы и одержите {amount} {noun}.",
                original,
            )

        match = re.fullmatch(
            r"Clear\s+any\s+Hard\s+Mode\s+stage\s+(\d+)\s+times?",
            normalized,
            re.IGNORECASE,
        )
        if match:
            amount = int(match.group(1))
            times = cls._ru_plural(amount, "раз", "раза", "раз")
            return (
                "daily",
                "Пройти этап в режиме Hard",
                f"Завершите любой этап Hard Mode {amount} {times}.",
                original,
            )

        match = re.fullmatch(
            r"Clear\s+([A-Z]+\d+)\s+or\s+([A-Z]+\d+)",
            normalized,
            re.IGNORECASE,
        )
        if match:
            left, right = match.group(1).upper(), match.group(2).upper()
            return (
                "event",
                f"Пройти {left} или {right}",
                "Завершите любой из указанных этапов текущего события.",
                original,
            )

        match = re.fullmatch(
            r"Clear\s+any\s+event\s+stage\s+(\d+)\s+times?",
            normalized,
            re.IGNORECASE,
        )
        if match:
            amount = int(match.group(1))
            times = cls._ru_plural(amount, "раз", "раза", "раз")
            return (
                "event",
                f"Пройти любой этап события {amount} {times}",
                f"Завершите любой этап текущего события суммарно {amount} {times}.",
                original,
            )

        group = "daily" if kind in _QUEST_DAILY_KINDS else "event"
        return (
            group,
            "Задание события",
            "Выполните условие задания, указанное в источнике события.",
            original,
        )

    @staticmethod
    def _event_currency(plan: Mapping[str, Any]) -> Mapping[str, Any]:
        for item in plan.get("currencies", []):
            if isinstance(item, Mapping):
                return item
        return {}

    def _render_event_profiles_compact(self, config: Mapping[str, Any]) -> None:
        profiles = get_event_profile_metadata(config)
        put_html(
            '<div class="event-general-compact-heading">'
            "<strong>Дополнительные профили</strong>"
            "</div>"
        )
        for slot in OPTIONAL_EVENT_PROFILE_SLOTS:
            profile = profiles.get(slot)
            if profile is None:
                continue
            put_row(
                [
                    put_html(
                        f'<strong class="event-general-profile-name">{escape(profile["name"])}</strong>'
                    ),
                    put_buttons(
                        [
                            {
                                "label": "Переименовать",
                                "value": "rename",
                                "color": "primary",
                            },
                            {
                                "label": "Удалить",
                                "value": "delete",
                                "color": "danger",
                            },
                        ],
                        onclick=[
                            partial(self._rename_event_profile, slot),
                            partial(self._delete_event_profile, slot),
                        ],
                    ),
                ],
                size="minmax(0, 1fr) auto",
            ).style("--event-general-profile-row--")
        if next_available_event_profile_slot(config) is not None:
            put_button(
                "Добавить профиль",
                onclick=self._add_event_profile,
                color="primary",
                disabled=is_demo_mode(),
            ).style("--event-general-profile-add--")
        else:
            put_html(
                '<small class="event-general-profile-limit">Лимит профилей достигнут.</small>'
            )

    def _scroll_event_rewards(self, direction: int) -> None:
        step = -1 if direction < 0 else 1
        run_js(
            f"""
(() => {{
  const track = document.getElementById("event-reward-track");
  if (!track) return;
  const amount = Math.max(track.clientWidth * 0.72, 320);
  track.scrollBy({{left: {step} * amount, behavior: "smooth"}});
}})();
"""
        )

    def _render_event_quest_group(
        self,
        *,
        title: str,
        description: str,
        items: list[Mapping[str, Any]],
        currency: Mapping[str, Any],
    ) -> None:
        if not items:
            return
        currency_icon = event_asset_url(currency.get("asset"))
        currency_name = escape(str(currency.get("name") or "Валюта события"))
        cards: list[str] = []
        for item in items:
            _, translated, explanation, _ = self._quest_presentation(item)
            points = item.get("points")
            points_text = self._fmt(points) if points is not None else "—"
            cards.append(
                '<article class="event-quest-card">'
                f'<strong>{escape(translated)}</strong>'
                f'<p>{escape(explanation)}</p>'
                '<div class="event-quest-reward">'
                f'<img src="{escape(currency_icon)}" alt="{currency_name}">'
                f'<b>{escape(points_text)}</b>'
                "</div>"
                "</article>"
            )
        put_html(
            '<section class="event-quest-group">'
            f'<div class="event-map-group-heading"><div><strong>{escape(title)}</strong>'
            f'<small>{escape(description)}</small></div>'
            f'<span class="event-subsection-count">{len(items)}</span></div>'
            f'<div class="event-quest-grid'>{\"\".join(cards)}</div>'
            "</section>"
        )

    def _render_event_rewards_v2(self) -> None:
        plan = self._event_plan()
        event = plan.get("event", {})
        if not isinstance(event, Mapping):
            event = {}
        current_pt, _ = self._event_progress(plan)
        milestones = [
            item for item in plan.get("milestones", []) if isinstance(item, Mapping)
        ]
        _, quests = self._split_event_sources(plan)
        currency = self._event_currency(plan)
        currency_icon = event_asset_url(currency.get("asset"))
        currency_name = escape(str(currency.get("name") or "Валюта события"))

        put_html(
            f"""
<section class="event-rewards-v2-hero">
  <div class="event-eyebrow">Награды текущего ивента</div>
  <h3>{escape(str(event.get("name") or "Текущий ивент"))}</h3>
  <span class="event-rewards-v2-balance">Текущий баланс: <img src="{escape(currency_icon)}" alt="{currency_name}"><strong>{self._fmt(current_pt) if current_pt is not None else "Нет данных"}</strong></span>
</section>
"""
        )
        put_html(
            '<div class="event-general-v2-section-heading event-rewards-heading">'
            '<div><strong>Награды за накопление</strong><small>Лента наград прокручивается по горизонтали.</small></div>'
            f'<span class="event-subsection-count">{len(milestones)}</span></div>'
        )
        put_row(
            [
                put_button(
                    "←",
                    onclick=partial(self._scroll_event_rewards, -1),
                    color="off",
                ),
                put_button(
                    "→",
                    onclick=partial(self._scroll_event_rewards, 1),
                    color="off",
                ),
            ],
            size="auto auto",
        ).style("--event-reward-track-controls--")

        next_threshold = min(
            (
                int(item.get("threshold", 0) or 0)
                for item in milestones
                if current_pt is not None
                and int(item.get("threshold", 0) or 0) > current_pt
            ),
            default=None,
        )
        cards: list[str] = []
        for milestone in milestones:
            threshold = int(milestone.get("threshold", 0) or 0)
            reached = current_pt is not None and current_pt >= threshold
            is_next = next_threshold is not None and threshold == next_threshold
            if reached:
                status = "Порог достигнут"
            elif current_pt is not None:
                status = f"Осталось {self._fmt(threshold - current_pt)}"
            else:
                status = "Прогресс пока недоступен"
            rewards = "".join(
                '<span class="event-reward-track-item">'
                f'<img src="{escape(event_asset_url(reward.get("asset")))}" alt="">'
                f'<span>{escape(str(reward.get("name") or "Награда"))}</span>'
                f'<b>× {escape(self._fmt(reward.get("amount")))}</b>'
                "</span>"
                for reward in milestone.get("rewards", [])
                if isinstance(reward, Mapping)
            )
            state_class = " event-reward-card-reached" if reached else ""
            if is_next:
                state_class += " event-reward-card-next"
            cards.append(
                f'<article class="event-reward-track-card{state_class}">'
                '<div class="event-reward-track-threshold"><small>Порог</small>'
                '<strong class="event-reward-threshold-value">'
                f'<img src="{escape(currency_icon)}" alt="{currency_name}">'
                f'<span>{self._fmt(threshold)}</span>'
                "</strong></div>"
                f'<div class="event-reward-track-items">{rewards or "<span>Нет данных о награде</span>"}</div>'
                f'<small class="event-reward-track-status">{escape(status)}</small>'
                "</article>"
            )
        if cards:
            put_html(
                '<div class="event-reward-track-shell">'
                f'<div id="event-reward-track" class="event-reward-track'>{\"\".join(cards)}</div>'
                "</div>"
            )
        else:
            put_html(
                '<div class="event-inline-empty">Награды пока не отображаются.</div>'
            )

        daily: list[Mapping[str, Any]] = []
        event_quests: list[Mapping[str, Any]] = []
        for item in quests:
            group, _, _, _ = self._quest_presentation(item)
            if group == "daily":
                daily.append(item)
            else:
                event_quests.append(item)

        if quests:
            self._render_event_quest_group(
                title="Ежедневные задания",
                description="Обновляются вместе с ежедневным циклом события.",
                items=daily,
                currency=currency,
            )
            self._render_event_quest_group(
                title="Задания события",
                description="Разовые и накопительные условия текущего события.",
                items=event_quests,
                currency=currency,
            )
        else:
            put_html(
                '<div class="event-inline-empty">Задания события пока не отображаются.</div>'
            )

    @use_scope("content", clear=True)
    def _alas_set_event_general_v2(self) -> None:
        config = self.alas_config.read_file(self.alas_name)
        self.init_menu(name="EventGeneral")
        self.set_title(EVENT_TASK_LABELS["EventGeneral"])
        put_scope("_groups", [put_none(), put_scope("groups"), put_scope("navigator")])
        self._mark_event_page("EventGeneral")
        self._event_plan_active_task = "EventGeneral"
        group_map = self._event_group_map(self.ALAS_ARGS["EventGeneral"])
        self._render_event_general_v2(config=config, group_map=group_map)

    @use_scope("content", clear=True)
    def _alas_set_event_rewards_v2(self) -> None:
        self.init_menu(name=EVENT_REWARDS_TASK)
        self.set_title(EVENT_TASK_LABELS[EVENT_REWARDS_TASK])
        put_scope("_groups", [put_none(), put_scope("groups"), put_scope("navigator")])
        self._mark_event_page(EVENT_REWARDS_TASK)
        self._event_plan_active_task = EVENT_REWARDS_TASK
        with use_scope("groups"):
            put_scope("group_EventRewards")
        with use_scope("group_EventRewards", clear=True):
            self._render_event_rewards_v2()
