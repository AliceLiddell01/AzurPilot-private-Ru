"""Final Stage 5 acceptance pass for current-event presentation and selector sync."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from html import escape
from typing import Any

import module.webui.lang as webui_lang
from module.event_datamine.campaign_selector import (
    EventCampaignSelectorError,
    generated_campaign_selector,
)
from module.logger import logger
from module.webui.app_dependencies import (
    current_time,
    deep_get,
    put_none,
    put_row,
    put_scope,
    t,
    use_scope,
)
from module.webui.app_event_layout import EVENT_MAP_TASKS
from module.webui.app_helpers import is_demo_mode
from module.webui.event_source import resolve_current_event_artifact


class EventAcceptanceMixin:
    """Acceptance fixes shared by EventGeneral and current Event map settings."""

    @staticmethod
    def _stage_title(stage: Mapping[str, Any]) -> str:
        code = str(stage.get("name") or "Этап").strip()
        title = str(stage.get("title") or "").strip()
        if not title or title.casefold() == code.casefold():
            return code
        return f"{code} — {title}"

    @staticmethod
    def _coin_range(stage: Mapping[str, Any]) -> tuple[int, int] | None:
        coins = stage.get("coins")
        if not isinstance(coins, Mapping):
            return None
        raw = coins.get("map_plus_clear_range")
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            return None
        left, right = raw
        if (
            isinstance(left, bool)
            or isinstance(right, bool)
            or not isinstance(left, int)
            or not isinstance(right, int)
            or left < 0
            or right < left
        ):
            return None
        return left, right

    def _format_coin_income(self, stage: Mapping[str, Any]) -> str | None:
        observed = stage.get("coin")
        if isinstance(observed, int) and not isinstance(observed, bool) and observed >= 0:
            return self._fmt(observed)
        verified_range = self._coin_range(stage)
        if verified_range is None:
            return None
        left, right = verified_range
        return f"{self._fmt(left)}–{self._fmt(right)}"

    @staticmethod
    def _reward_items(value: Any) -> list[tuple[str, Any]]:
        if not isinstance(value, list):
            return []
        result: list[tuple[str, Any]] = []
        for item in value:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            name = str(item[0] or "").strip()
            if name:
                result.append((name, item[1]))
        return result

    def _reward_line(self, title: str, value: Any) -> str:
        rewards = self._reward_items(value)
        if not rewards:
            return ""
        rendered = ", ".join(
            f"{escape(name)} × {escape(self._fmt(amount))}" for name, amount in rewards
        )
        return (
            '<div class="event-farm-reward-line">'
            f'<small>{escape(title)}</small><span>{rendered}</span></div>'
        )

    @staticmethod
    def _pt_source_sort_key(source: Mapping[str, Any]) -> tuple[int, str]:
        order = {
            "repeatable_map_clear": 0,
            "daily_first_clear": 1,
            "first_clear": 2,
            "challenge": 3,
        }
        kind = str(source.get("kind") or "")
        return order.get(kind, 9), str(source.get("id") or "")

    @staticmethod
    def _pt_source_caption(source: Mapping[str, Any]) -> tuple[str, str]:
        kind = str(source.get("kind") or "")
        multiplier = source.get("multiplier")
        daily_limit = source.get("daily_limit")
        if kind == "repeatable_map_clear":
            return "Обычное прохождение", ""
        if kind == "daily_first_clear":
            if isinstance(multiplier, int) and not isinstance(multiplier, bool) and multiplier > 1:
                return "Первое прохождение дня", f"×{multiplier}"
            if isinstance(daily_limit, int) and not isinstance(daily_limit, bool) and daily_limit == 1:
                return "Одно прохождение в день", ""
            return "Ежедневное прохождение", ""
        if kind == "first_clear":
            return "Первое прохождение", ""
        if kind == "challenge":
            return "Испытание", ""
        return "Источник PT", ""

    def _combined_map_pt_sources(
        self,
        plan: Mapping[str, Any],
        sources: list[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        stages = {
            str(stage.get("id") or ""): stage
            for stage in plan.get("stages", [])
            if isinstance(stage, Mapping)
        }
        combined: dict[tuple[str, str], dict[str, Any]] = {}
        for source in sources:
            source_ids = list(source.get("source_ids") or [])
            map_id = str(source_ids[0]) if len(source_ids) == 1 else ""
            stage = stages.get(map_id, {})
            code = str(stage.get("name") or source.get("name") or "Этап")
            title = str(stage.get("title") or "").strip()
            identity = map_id or f"name:{code}"
            key = identity, code
            card = combined.setdefault(
                key,
                {
                    "name": code,
                    "title": title,
                    "map_id": map_id,
                    "sources": [],
                },
            )
            card["sources"].append(source)
        for card in combined.values():
            card["sources"].sort(key=self._pt_source_sort_key)
        return list(combined.values())

    def _render_source_card(self, card: Mapping[str, Any]) -> str:
        code = str(card.get("name") or "Этап")
        title = str(card.get("title") or "").strip()
        heading = (
            f'<strong>{escape(code)}</strong><span class="event-stage-title">{escape(title)}</span>'
            if title and title.casefold() != code.casefold()
            else f'<strong>{escape(code)}</strong>'
        )
        rows: list[str] = []
        for source in card.get("sources", []):
            if not isinstance(source, Mapping):
                continue
            caption, suffix = self._pt_source_caption(source)
            points = source.get("points")
            value = self._fmt(points) if points is not None else "Нет данных"
            suffix_html = f'<em>{escape(suffix)}</em>' if suffix else ""
            rows.append(
                '<span class="event-source-value-row">'
                f'<small>{escape(caption)}</small><b>{escape(value)} PT {suffix_html}</b></span>'
            )
        return (
            '<article class="event-source-card event-source-card-v2 event-source-card-combined">'
            f'<div class="event-source-card-heading">{heading}</div>'
            f'<div class="event-source-values">{"".join(rows)}</div></article>'
        )

    def _render_event_sources_v2(self, plan: Mapping[str, Any]) -> None:
        overview, _ = self._split_event_sources(plan)
        map_sources = [
            item
            for item in overview
            if item.get("kind") == "repeatable_map_clear"
            or self._map_group_key(item.get("name")) != "OTHER"
        ]
        other_sources = [item for item in overview if item not in map_sources]
        combined = self._combined_map_pt_sources(plan, map_sources)

        from module.webui.app_dependencies import put_html

        put_html(
            '<div class="event-general-v2-section-heading"><div>'
            "<strong>Источники PT</strong>"
            "<small>Одна карточка на этап: обычная и ежедневная награда показаны вместе.</small>"
            "</div></div>"
        )

        rendered = False
        for title, subtitle, rows in self._group_map_items(combined):
            rendered = True
            special = self._map_group_key(rows[0].get("name")) == "SPECIAL"
            section_class = " event-map-group-special" if special else ""
            cards = "".join(self._render_source_card(item) for item in rows)
            put_html(
                f'<section class="event-map-group{section_class}">'
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
            put_html('<div class="event-inline-empty">Источники PT пока не отображаются.</div>')

    @staticmethod
    def _stage_presentation_signature(stage: Mapping[str, Any]) -> str:
        payload = {
            key: stage.get(key)
            for key in (
                "name",
                "title",
                "mode",
                "points",
                "oil",
                "coins",
                "clear_rewards",
                "three_star_rewards",
                "required_battles",
                "daily_limit",
                "grants_event_pt",
            )
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)

    def _user_facing_stages(self, plan: Mapping[str, Any]) -> list[dict[str, Any]]:
        unique: dict[str, dict[str, Any]] = {}
        for raw in plan.get("stages", []):
            if not isinstance(raw, Mapping):
                continue
            stage = copy.deepcopy(dict(raw))
            signature = self._stage_presentation_signature(stage)
            existing = unique.get(signature)
            if existing is None:
                stage["variant_ids"] = [str(stage.get("id") or "")]
                unique[signature] = stage
                continue
            identity = str(stage.get("id") or "")
            if identity and identity not in existing["variant_ids"]:
                existing["variant_ids"].append(identity)
        return list(unique.values())

    def _render_farm_card(self, stage: Mapping[str, Any], remaining_pt: int | None) -> str:
        points = stage.get("points")
        runs = (
            None
            if not isinstance(points, int)
            or isinstance(points, bool)
            or points <= 0
            or remaining_pt is None
            else (remaining_pt + points - 1) // points
        )
        code = str(stage.get("name") or "Этап")
        title = str(stage.get("title") or "").strip()
        title_html = (
            f'<span class="event-stage-title">{escape(title)}</span>'
            if title and title.casefold() != code.casefold()
            else ""
        )

        income: list[str] = []
        if points is not None:
            income.append(
                f'<span><small>PT</small><b>{escape(self._fmt(points))}</b></span>'
            )
        coin_text = self._format_coin_income(stage)
        if coin_text is not None:
            income.append(
                f'<span><small>Монеты</small><b>{escape(coin_text)}</b></span>'
            )
        cost: list[str] = []
        oil = stage.get("oil")
        if oil is not None:
            cost.append(f'<span><small>Нефть</small><b>{escape(self._fmt(oil))}</b></span>')
        planning: list[str] = []
        if runs is not None:
            planning.append(
                f'<span><small>Проходы</small><b>{escape(self._fmt(runs))}</b></span>'
            )
        required_battles = stage.get("required_battles")
        if isinstance(required_battles, int) and not isinstance(required_battles, bool):
            planning.append(
                f'<span><small>Боёв для зачистки</small><b>{escape(self._fmt(required_battles))}</b></span>'
            )

        blocks: list[str] = []
        if income:
            blocks.append(
                '<div class="event-farm-block"><small class="event-farm-block-title">Доход за проход</small>'
                f'<div class="event-farm-facts">{"".join(income)}</div></div>'
            )
        if cost:
            blocks.append(
                '<div class="event-farm-block"><small class="event-farm-block-title">Затраты</small>'
                f'<div class="event-farm-facts">{"".join(cost)}</div></div>'
            )
        if planning:
            blocks.append(
                '<div class="event-farm-block"><small class="event-farm-block-title">Планирование</small>'
                f'<div class="event-farm-facts">{"".join(planning)}</div></div>'
            )

        rewards = self._reward_line("Награда за первое прохождение", stage.get("clear_rewards"))
        rewards += self._reward_line("Награда за 3★", stage.get("three_star_rewards"))
        return (
            '<article class="event-farm-card event-farm-card-v2">'
            f'<div class="event-farm-card-head"><strong>{escape(code)}</strong>{title_html}</div>'
            f'<div class="event-farm-sections">{"".join(blocks)}</div>{rewards}</article>'
        )

    def _render_event_stages_v2(
        self,
        *,
        plan: Mapping[str, Any],
        remaining_pt: int | None,
    ) -> None:
        from module.webui.app_dependencies import put_html

        put_html(
            '<div class="event-general-v2-section-heading"><div>'
            "<strong>Этапы фарма</strong>"
            "<small>Доход, затраты и разовые награды разделены по смыслу.</small>"
            "</div></div>"
        )
        stages = self._user_facing_stages(plan)
        rendered = False
        for title, subtitle, rows in self._group_map_items(stages):
            rendered = True
            special = self._map_group_key(rows[0].get("name")) == "SPECIAL"
            section_class = " event-map-group-special" if special else ""
            cards = "".join(self._render_farm_card(stage, remaining_pt) for stage in rows)
            put_html(
                f'<section class="event-map-group{section_class}">'
                f'<div class="event-map-group-heading"><div><strong>{escape(title)}</strong>'
                f'<small>{escape(subtitle)}</small></div>'
                f'<span class="event-subsection-count">{len(rows)}</span></div>'
                f'<div class="event-farm-grid event-farm-grid-v2">{cards}</div>'
                "</section>"
            )
        if not rendered:
            put_html('<div class="event-inline-empty">Этапы фарма пока не отображаются.</div>')

    def _render_event_general_v2(
        self,
        *,
        config: Mapping[str, Any],
        group_map: Mapping[str, Any],
    ) -> None:
        plan = self._event_plan()
        with use_scope("groups"):
            put_row(
                [put_scope("group_EventMainColumn"), put_scope("group_EventSideColumn")],
                size="minmax(0, 1fr) minmax(330px, 360px)",
            ).style("--event-general-v2-layout--")
            put_scope("group_EventSources")
            put_scope("group_EventStages")

        with use_scope("group_EventMainColumn"):
            put_scope("group_EventOverview")
        with use_scope("group_EventSideColumn"):
            put_scope("group_EventProfiles")
            put_scope("group_TaskBalancer")

        with use_scope("group_EventOverview", clear=True):
            _, remaining_pt = self._render_event_overview_summary(plan=plan, config=config)
        with use_scope("group_EventProfiles", clear=True):
            self._render_event_profiles_compact(config)
        self._render_named_group("EventGeneral", "TaskBalancer", group_map, config, False)
        with use_scope("group_EventSources", clear=True):
            self._render_event_sources_v2(plan)
        with use_scope("group_EventStages", clear=True):
            self._render_event_stages_v2(plan=plan, remaining_pt=remaining_pt)

    def _current_event_campaign(self) -> tuple[str, str] | None:
        if is_demo_mode():
            return None
        artifact, unavailable = resolve_current_event_artifact(
            server="EN", now=current_time()
        )
        if artifact is None:
            if unavailable:
                logger.warning("[WebUI — ивент] Current campaign selector недоступен")
            return None
        try:
            selector = generated_campaign_selector(artifact)
        except EventCampaignSelectorError as exc:
            logger.warning(f"[WebUI — ивент] Generated campaign selector отклонён: {exc}")
            return None
        spec = artifact.get("event_spec")
        if not isinstance(spec, Mapping):
            return None
        name = str(spec.get("name") or selector)
        return selector, name

    def _prepare_event_map_args(
        self,
        task: str,
        config: Mapping[str, Any],
    ) -> tuple[dict[str, Any], Mapping[str, Any]]:
        task_args = copy.deepcopy(dict(self.ALAS_ARGS[task]))
        current = self._current_event_campaign()
        if current is None:
            return task_args, config
        selector, event_name = current
        campaign = task_args.get("Campaign")
        if not isinstance(campaign, dict):
            return task_args, config
        event_arg = campaign.get("Event")
        if not isinstance(event_arg, dict):
            return task_args, config

        event_arg["value"] = selector
        event_arg["option"] = [selector]
        event_arg["option_en"] = [selector]
        event_arg["option_bold"] = [selector]
        webui_lang.dic_lang[f"Campaign.Event.{selector}"] = event_name

        saved = deep_get(config, [task, "Campaign", "Event"], None)
        if saved != selector:
            self._event_config_update({f"{task}.Campaign.Event": selector})
            config = self.alas_config.read_file(self.alas_name)
        return task_args, config

    @use_scope("content", clear=True)
    def _alas_set_event_group(self, task: str) -> None:
        config = self.alas_config.read_file(self.alas_name)
        task_args: Mapping[str, Any] = self.ALAS_ARGS[task]
        if task in EVENT_MAP_TASKS:
            task_args, config = self._prepare_event_map_args(task, config)

        self.init_menu(name=task)
        self.set_title(t(f"Task.{task}.name"))
        put_scope("_groups", [put_none(), put_scope("groups"), put_scope("navigator")])
        self._mark_event_page(task)
        group_map = self._event_group_map(dict(task_args))
        if task == "EventGeneral":
            self._event_plan_active_task = task
            self._render_event_general_layout(task=task, group_map=group_map, config=config)
        elif task in EVENT_MAP_TASKS:
            self._event_plan_active_task = task
            self._render_event_map_layout(task=task, group_map=group_map, config=config)
        elif task == "EventShop":
            self._event_plan_active_task = task
            self._render_event_shop_layout(task=task, group_map=group_map, config=config)
