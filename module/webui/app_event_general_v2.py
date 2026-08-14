"""Focused EventGeneral WebUI: compact overview plus a separate rewards surface."""

from __future__ import annotations

import re
from collections.abc import Mapping
from functools import partial
from html import escape
from typing import Any

from module.webui.app_dependencies import (
    deep_get,
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
from module.webui.event_plan import shop_plan_total
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
    """Present EventGeneral as user-facing information without changing runtime tasks."""

    def _ensure_event_rewards_menu_entry(self) -> None:
        """Add the WebUI-only rewards page to this session's Event menu."""
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
        """Keep profile CRUD on the compact EventGeneral surface."""
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
        """Return group, Russian title, Russian description and original source text."""
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

    def _render_event_overview_summary(
        self,
        *,
        plan: Mapping[str, Any],
        config: Mapping[str, Any],
    ) -> tuple[int | None, int | None]:
        event = plan.get("event", {})
        if not isinstance(event, Mapping):
            event = {}
        current_pt, _ = self._event_progress(plan)
        target = int(deep_get(config, "EventGeneral.EventGeneral.PtLimit", 0) or 0)
        shop_total = shop_plan_total(plan)
        planning_target = max(target, shop_total)
        remaining = (
            max(planning_target - current_pt, 0)
            if planning_target > 0 and isinstance(current_pt, int)
            else None
        )
        progress = (
            max(0, min(100, round(current_pt * 100 / planning_target)))
            if planning_target > 0 and isinstance(current_pt, int)
            else 0
        )
        farm_start = escape(str(event.get("farm_start") or "Не задано"))
        farm_end = escape(str(event.get("farm_end") or "Не задано"))
        shop_end = escape(str(event.get("shop_end") or "Не задано"))

        put_html(
            f"""
<section class="event-general-v2-hero">
  <div class="event-eyebrow">Текущий ивент · {escape(str(event.get("server") or "EN"))}</div>
  <h3>{escape(str(event.get("name") or "Текущий ивент"))}</h3>
  <div class="event-general-v2-dates">
    <span>Фарм <strong>{farm_start}</strong> — <strong>{farm_end}</strong></span>
    <span>Магазин до <strong>{shop_end}</strong></span>
  </div>
  <div class="event-general-v2-metrics">
    <div class="event-general-v2-metric event-general-v2-metric-accent"><small>Текущий PT</small><strong>{self._fmt(current_pt) if current_pt is not None else "Нет данных"}</strong><span>Обновляется автоматически</span></div>
    <div class="event-general-v2-metric"><small>Цель фарма</small><strong>{self._fmt(planning_target) + " PT" if planning_target else "Не задана"}</strong><span>Настраивается вручную</span></div>
    <div class="event-general-v2-metric"><small>Осталось набрать</small><strong>{self._fmt(remaining) + " PT" if remaining is not None else "—"}</strong><span>До текущей цели</span></div>
    <div class="event-general-v2-metric"><small>Прогресс</small><strong>{str(progress) + "%" if current_pt is not None and planning_target else "—"}</strong><span>По текущему балансу PT</span></div>
  </div>
  <div class="event-general-v2-progress" aria-label="Прогресс к цели"><span style="width:{progress}%"></span></div>
</section>
"""
        )
        put_row(
            [
                put_button(
                    "Настроить цель фарма",
                    onclick=self._settings_popup,
                    color="primary",
                )
            ],
            size="auto",
        ).style("--event-general-v2-primary-action--")
        return current_pt, remaining

    def _render_event_profiles_compact(self, config: Mapping[str, Any]) -> None:
        profiles = get_event_profile_metadata(config)
        put_html(
            '<div class="event-general-compact-heading">'
            "<strong>Дополнительные ивентовые профили</strong>"
            "<small>До двух независимых профилей карт со своими настройками.</small>"
            "</div>"
        )
        if not profiles:
            put_html(
                '<div class="event-general-compact-empty">Дополнительные профили не созданы.</div>'
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
                "Добавить доп. ивентовый профиль",
                onclick=self._add_event_profile,
                color="primary",
                disabled=is_demo_mode(),
            ).style("--event-general-profile-add--")
        else:
            put_html(
                '<small class="event-general-profile-limit">Доступно не более двух дополнительных профилей.</small>'
            )

    def _render_event_sources_v2(self, plan: Mapping[str, Any]) -> None:
        overview, _ = self._split_event_sources(plan)
        stage_sources = [
            item
            for item in overview
            if item.get("kind") == "repeatable_map_clear"
            or self._map_group_key(item.get("name")) != "OTHER"
        ]
        other_sources = [item for item in overview if item not in stage_sources]

        put_html(
            '<div class="event-general-v2-section-heading"><div>'
            "<strong>Источники PT</strong>"
            "<small>Карты сгруппированы по веткам события и сложности.</small>"
            "</div></div>"
        )

        rendered = False
        for title, subtitle, rows in self._group_map_items(stage_sources):
            rendered = True
            cards = "".join(
                '<article class="event-source-card event-source-card-v2">'
                '<span class="event-source-kind">Награда за прохождение</span>'
                f'<strong>{escape(str(item.get("name") or "Этап"))}</strong>'
                f'<b>{escape(self._fmt(item["points"]) if item.get("points") is not None else "Нет данных")} PT</b>'
                "</article>"
                for item in rows
            )
            put_html(
                '<section class="event-map-group">'
                f'<div class="event-map-group-heading"><div><strong>{escape(title)}</strong>'
                f'<small>{escape(subtitle)}</small></div>'
                f'<span class="event-subsection-count">{len(rows)}</span></div>'
                f'<div class="event-source-grid event-source-grid-v2">{cards}</div>'
                "</section>"
            )

        if other_sources:
            rendered = True
            cards = "".join(
                '<article class="event-source-card event-source-card-v2">'
                '<span class="event-source-kind">Источник PT</span>'
                f'<strong>{escape(str(item.get("name") or "Источник"))}</strong>'
                f'<b>{escape(self._fmt(item["points"]) if item.get("points") is not None else "Нет данных")} PT</b>'
                "</article>"
                for item in other_sources
            )
            put_html(
                '<section class="event-map-group">'
                '<div class="event-map-group-heading"><div><strong>Другие источники PT</strong>'
                '<small>Источники, не относящиеся к отдельной карте.</small></div>'
                f'<span class="event-subsection-count">{len(other_sources)}</span></div>'
                f'<div class="event-source-grid event-source-grid-v2">{cards}</div>'
                "</section>"
            )

        if not rendered:
            put_html(
                '<div class="event-inline-empty">Источники PT пока не отображаются.</div>'
            )

    def _render_event_stages_v2(
        self,
        *,
        plan: Mapping[str, Any],
        remaining_pt: int | None,
    ) -> None:
        put_html(
            '<div class="event-general-v2-section-heading"><div>'
            "<strong>Этапы фарма</strong>"
            "<small>Этапы сгруппированы так же, как карты события.</small>"
            "</div></div>"
        )

        stages = [
            stage for stage in plan.get("stages", []) if isinstance(stage, Mapping)
        ]
        rendered = False
        for title, subtitle, rows in self._group_map_items(stages):
            rendered = True
            cards: list[str] = []
            for stage in rows:
                points = stage.get("points")
                runs = (
                    "—"
                    if not isinstance(points, int)
                    or points <= 0
                    or remaining_pt is None
                    else self._fmt((remaining_pt + points - 1) // points)
                )
                cards.append(
                    '<article class="event-farm-card event-farm-card-v2">'
                    f'<div class="event-farm-card-head"><strong>{escape(str(stage.get("name") or "Этап"))}</strong></div>'
                    '<div class="event-farm-facts">'
                    f'<span><small>PT</small><b>{escape(self._fmt(points) if points is not None else "Нет данных")}</b></span>'
                    f'<span><small>Нефть</small><b>{escape(self._fmt(stage.get("oil")) if stage.get("oil") is not None else "Нет данных")}</b></span>'
                    f'<span><small>Монеты</small><b>{escape(self._fmt(stage.get("coin")) if stage.get("coin") is not None else "Нет данных")}</b></span>'
                    f'<span><small>Звёзды</small><b>{escape(str(stage.get("stars")) if stage.get("stars") is not None else "Нет данных")}</b></span>'
                    f'<span><small>Проходы</small><b>{escape(runs)}</b></span>'
                    "</div></article>"
                )
            put_html(
                '<section class="event-map-group">'
                f'<div class="event-map-group-heading"><div><strong>{escape(title)}</strong>'
                f'<small>{escape(subtitle)}</small></div>'
                f'<span class="event-subsection-count">{len(rows)}</span></div>'
                f'<div class="event-farm-grid event-farm-grid-v2'>{"".join(cards)}</div>'
                "</section>"
            )

        if not rendered:
            put_html(
                '<div class="event-inline-empty">Этапы фарма пока не отображаются.</div>'
            )

    def _render_event_general_v2(
        self,
        *,
        config: Mapping[str, Any],
        group_map: Mapping[str, Any],
    ) -> None:
        plan = self._event_plan()
        with use_scope("groups"):
            put_row(
                [
                    put_scope("group_EventMainColumn"),
                    put_scope("group_EventSideColumn"),
                ],
                size="minmax(0, 1fr) minmax(330px, 360px)",
            ).style("--event-general-v2-layout--")

        with use_scope("group_EventMainColumn"):
            put_scope("group_EventOverview")
            put_scope("group_EventSources")
            put_scope("group_EventStages")

        with use_scope("group_EventSideColumn"):
            put_scope("group_EventProfiles")
            put_scope("group_TaskBalancer")

        with use_scope("group_EventOverview", clear=True):
            _, remaining_pt = self._render_event_overview_summary(
                plan=plan, config=config
            )

        with use_scope("group_EventProfiles", clear=True):
            self._render_event_profiles_compact(config)

        self._render_named_group(
            "EventGeneral",
            "TaskBalancer",
            group_map,
            config,
            False,
        )

        with use_scope("group_EventSources", clear=True):
            self._render_event_sources_v2(plan)

        with use_scope("group_EventStages", clear=True):
            self._render_event_stages_v2(plan=plan, remaining_pt=remaining_pt)

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
            _, translated, explanation, original = self._quest_presentation(item)
            points = item.get("points")
            points_text = self._fmt(points) if points is not None else "—"
            original_html = (
                f'<small class="event-quest-original">Оригинал: {escape(original)}</small>'
                if original
                else ""
            )
            cards.append(
                '<article class="event-quest-card">'
                f'<strong>{escape(translated)}</strong>'
                f'<p>{escape(explanation)}</p>'
                f"{original_html}"
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
            f'<div class="event-quest-grid'>{"".join(cards)}</div>'
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

        put_html(
            f"""
<section class="event-rewards-v2-hero">
  <div class="event-eyebrow">Награды текущего ивента</div>
  <h3>{escape(str(event.get("name") or "Текущий ивент"))}</h3>
  <span>Текущий PT: <strong>{self._fmt(current_pt) if current_pt is not None else "Нет данных"}</strong></span>
</section>
"""
        )
        put_html(
            '<div class="event-general-v2-section-heading event-rewards-heading">'
            '<div><strong>Награды за накопление PT</strong><small>Крупная лента наград прокручивается по горизонтали.</small></div>'
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

        next_threshold = next(
            (
                int(item.get("threshold", 0) or 0)
                for item in milestones
                if current_pt is not None
                and int(item.get("threshold", 0) or 0) > current_pt
            ),
            None,
        )
        cards: list[str] = []
        for milestone in milestones:
            threshold = int(milestone.get("threshold", 0) or 0)
            reached = current_pt is not None and current_pt >= threshold
            is_next = next_threshold is not None and threshold == next_threshold
            if reached:
                status = "Порог достигнут"
            elif current_pt is not None:
                status = f"Осталось {self._fmt(threshold - current_pt)} PT"
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
                f'<div class="event-reward-track-threshold"><small>Порог</small><strong>{self._fmt(threshold)} PT</strong></div>'
                f'<div class="event-reward-track-items">{rewards or "<span>Нет данных о награде</span>"}</div>'
                f'<small class="event-reward-track-status">{escape(status)}</small>'
                "</article>"
            )
        if cards:
            put_html(
                '<div class="event-reward-track-shell">'
                f'<div id="event-reward-track" class="event-reward-track">{"".join(cards)}</div>'
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

        put_html(
            '<div class="event-general-v2-section-heading event-quest-heading">'
            '<div><strong>Задания</strong><small>Без попыток определять состояние выполнения.</small></div>'
            f'<span class="event-subsection-count">{len(quests)}</span></div>'
        )
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
