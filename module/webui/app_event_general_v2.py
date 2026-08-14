"""Focused EventGeneral WebUI: compact overview plus a separate rewards surface."""

from __future__ import annotations

from collections.abc import Mapping
from functools import partial
from html import escape
from typing import Any

from module.webui.app_dependencies import (
    deep_get,
    put_button,
    put_buttons,
    put_html,
    put_none,
    put_row,
    put_scope,
    run_js,
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
        quests = [item for item in sources if item.get("kind") == "unknown"]
        overview = [item for item in sources if item.get("kind") != "unknown"]
        return overview, quests

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
            '<strong>Дополнительные ивентовые профили</strong>'
            '<small>До двух независимых профилей карт со своими настройками.</small>'
            '</div>'
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
        labels = {
            "daily": "Ежедневные задания",
            "weekly": "Еженедельные задания",
            "one_time": "Разовые задания",
            "first_clear": "Первое прохождение",
            "daily_first_clear": "Ежедневное первое прохождение",
            "repeatable_map_clear": "Повторяемый фарм карт",
            "challenge": "Испытания",
        }
        overview, _ = self._split_event_sources(plan)
        put_html(
            '<div class="event-general-v2-section-heading"><strong>Источники PT</strong>'
            '<small>Доступные способы получения PT в текущем событии.</small></div>'
        )
        rendered = False
        for kind, title in labels.items():
            rows = [item for item in overview if item.get("kind") == kind]
            if not rows:
                continue
            rendered = True
            cards = "".join(
                '<article class="event-source-card event-source-card-v2">'
                f'<span class="event-source-kind">{escape(title)}</span>'
                f'<strong>{escape(str(item.get("name") or title))}</strong>'
                f'<b>{escape(self._fmt(item["points"]) if item.get("points") is not None else "Нет данных")} PT</b>'
                '</article>'
                for item in rows
            )
            put_html(
                f'<div class="event-subsection-heading"><span>{escape(title)}</span>'
                f'<span class="event-subsection-count">{len(rows)}</span></div>'
                f'<div class="event-source-grid">{cards}</div>'
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
            '<div class="event-general-v2-section-heading"><strong>Этапы фарма</strong>'
            '<small>Прогноз проходов рассчитывается по текущей цели.</small></div>'
        )
        cards: list[str] = []
        for stage in plan.get("stages", []):
            if not isinstance(stage, Mapping):
                continue
            points = stage.get("points")
            runs = (
                "—"
                if not isinstance(points, int) or points <= 0 or remaining_pt is None
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
                '</div></article>'
            )
        if cards:
            put_html(f'<div class="event-farm-grid">{"".join(cards)}</div>')
        else:
            put_html('<div class="event-inline-empty">Этапы фарма пока не отображаются.</div>')

    def _render_event_general_v2(
        self,
        *,
        config: Mapping[str, Any],
        group_map: Mapping[str, Any],
    ) -> None:
        plan = self._event_plan()
        with use_scope("groups"):
            put_scope("group_EventOverview")
        with use_scope("group_EventOverview", clear=True):
            _, remaining_pt = self._render_event_overview_summary(plan=plan, config=config)

        with use_scope("groups"):
            put_scope("group_EventProfiles")
        with use_scope("group_EventProfiles", clear=True):
            self._render_event_profiles_compact(config)

        self._render_named_group(
            "EventGeneral",
            "TaskBalancer",
            group_map,
            config,
            False,
        )

        with use_scope("groups"):
            put_scope("group_EventSources")
        with use_scope("group_EventSources", clear=True):
            self._render_event_sources_v2(plan)

        with use_scope("groups"):
            put_scope("group_EventStages")
        with use_scope("group_EventStages", clear=True):
            self._render_event_stages_v2(plan=plan, remaining_pt=remaining_pt)

    def _scroll_event_rewards(self, direction: int) -> None:
        step = -1 if direction < 0 else 1
        run_js(
            f"""
(() => {{
  const track = document.getElementById("event-reward-track");
  if (!track) return;
  const amount = Math.max(track.clientWidth * 0.72, 260);
  track.scrollBy({{left: {step} * amount, behavior: "smooth"}});
}})();
"""
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
            '<div><strong>Награды за накопление PT</strong><small>Прокручивайте ленту по горизонтали.</small></div>'
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
                '</span>'
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
                '</article>'
            )
        if cards:
            put_html(
                f'<div id="event-reward-track" class="event-reward-track">{"".join(cards)}</div>'
            )
        else:
            put_html('<div class="event-inline-empty">Награды пока не отображаются.</div>')

        put_html(
            '<div class="event-general-v2-section-heading event-quest-heading">'
            '<strong>Задания события</strong>'
            f'<small>{len(quests)} заданий</small></div>'
        )
        if quests:
            cards = "".join(
                '<article class="event-quest-card">'
                f'<strong>{escape(str(item.get("name") or "Задание события"))}</strong>'
                f'<b>{escape(self._fmt(item["points"]) if item.get("points") is not None else "Нет данных")} PT</b>'
                '</article>'
                for item in quests
            )
            put_html(f'<div class="event-quest-grid">{cards}</div>')
        else:
            put_html('<div class="event-inline-empty">Задания события пока не отображаются.</div>')

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
