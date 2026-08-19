"""Каноническое пользовательское представление EventGeneral."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from html import escape
from typing import Any

from module.webui.app_dependencies import (
    deep_get,
    put_button,
    put_html,
    put_row,
    put_scope,
    use_scope,
)
from module.webui.event_assets import event_asset_url
from module.webui.event_plan import shop_plan_total


class EventGeneralPresentationMixin:
    """Рендерить источники валюты, фарм и обзор события без временных overrides."""

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

    def _event_currency_markup(
        self,
        plan: Mapping[str, Any],
        *,
        css_class: str = "event-currency-inline-icon",
    ) -> str:
        currency = self._event_currency(plan)
        asset = currency.get("asset") if isinstance(currency, Mapping) else None
        url = event_asset_url(asset if isinstance(asset, Mapping) else None)
        name = (
            str(currency.get("name") or "Валюта события")
            if isinstance(currency, Mapping)
            else "Валюта события"
        )
        return (
            f'<img class="{escape(css_class)}" src="{escape(url)}" '
            f'alt="{escape(name)}" title="{escape(name)}">'
        )

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
            if (
                isinstance(multiplier, int)
                and not isinstance(multiplier, bool)
                and multiplier > 1
            ):
                return "Первое прохождение дня", f"×{multiplier}"
            if (
                isinstance(daily_limit, int)
                and not isinstance(daily_limit, bool)
                and daily_limit == 1
            ):
                return "Одно прохождение в день", ""
            return "Ежедневное прохождение", ""
        if kind == "first_clear":
            return "Первое прохождение", ""
        if kind == "challenge":
            return "Испытание", ""
        return "Источник валюты", ""

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

    def _render_source_card(
        self,
        card: Mapping[str, Any],
        currency_markup: str = "",
    ) -> str:
        code = str(card.get("name") or "Этап")
        title = str(card.get("title") or "").strip()
        heading = (
            f'<strong>{escape(code)}</strong><span class="event-stage-title">{escape(title)}</span>'
            if title and title.casefold() != code.casefold()
            else f'<strong>{escape(code)}</strong>'
        )
        currency = currency_markup or '<span class="event-currency-inline-fallback">PT</span>'
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
                f'<small>{escape(caption)}</small>'
                f'<b class="event-currency-value">{currency}{escape(value)}{suffix_html}</b></span>'
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
        currency_markup = self._event_currency_markup(plan)

        put_html(
            '<div class="event-general-v2-section-heading"><div>'
            "<strong>Источники валюты события</strong>"
            "<small>Одна карточка на этап: обычная и ежедневная награда показаны вместе.</small>"
            "</div></div>"
        )

        rendered = False
        for title, subtitle, rows in self._group_map_items(combined):
            rendered = True
            special = self._map_group_key(rows[0].get("name")) == "SPECIAL"
            section_class = " event-map-group-special" if special else ""
            cards = "".join(
                self._render_source_card(item, currency_markup) for item in rows
            )
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
                '<span class="event-source-kind">Источник валюты</span>'
                f'<strong>{escape(str(item.get("name") or "Источник"))}</strong>'
                f'<b class="event-currency-value">{currency_markup}{escape(self._fmt(item["points"]) if item.get("points") is not None else "Нет данных")}</b>'
                "</article>"
                for item in other_sources
            )
            put_html(
                '<section class="event-map-group">'
                '<div class="event-map-group-heading"><div><strong>Другие источники валюты</strong>'
                '<small>Источники, не относящиеся к отдельной карте.</small></div>'
                f'<span class="event-subsection-count">{len(other_sources)}</span></div>'
                f'<div class="event-source-grid event-source-grid-v2">{cards}</div>'
                "</section>"
            )

        if not rendered:
            put_html(
                '<div class="event-inline-empty">Источники валюты пока не отображаются.</div>'
            )

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
                "coin",
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

    def _render_farm_card(
        self,
        stage: Mapping[str, Any],
        remaining_pt: int | None,
        currency_markup: str = "",
    ) -> str:
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
            currency = currency_markup or '<span class="event-currency-inline-fallback">PT</span>'
            income.append(
                '<span><small>Валюта события</small>'
                f'<b class="event-currency-value">{currency}{escape(self._fmt(points))}</b></span>'
            )
        coin_text = self._format_coin_income(stage)
        if coin_text is not None:
            income.append(
                f'<span><small>Монеты</small><b>{escape(coin_text)}</b></span>'
            )
        cost: list[str] = []
        oil = stage.get("oil")
        if oil is not None:
            cost.append(
                f'<span><small>Нефть</small><b>{escape(self._fmt(oil))}</b></span>'
            )
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

        rewards = self._reward_line(
            "Награда за первое прохождение", stage.get("clear_rewards")
        )
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
        put_html(
            '<div class="event-general-v2-section-heading"><div>'
            "<strong>Этапы фарма</strong>"
            "<small>Доход, затраты и разовые награды разделены по смыслу.</small>"
            "</div></div>"
        )
        stages = self._user_facing_stages(plan)
        currency_markup = self._event_currency_markup(plan)
        rendered = False
        for title, subtitle, rows in self._group_map_items(stages):
            rendered = True
            special = self._map_group_key(rows[0].get("name")) == "SPECIAL"
            section_class = " event-map-group-special" if special else ""
            cards = "".join(
                self._render_farm_card(stage, remaining_pt, currency_markup)
                for stage in rows
            )
            put_html(
                f'<section class="event-map-group{section_class}">'
                f'<div class="event-map-group-heading"><div><strong>{escape(title)}</strong>'
                f'<small>{escape(subtitle)}</small></div>'
                f'<span class="event-subsection-count">{len(rows)}</span></div>'
                f'<div class="event-farm-grid event-farm-grid-v2">{cards}</div>'
                "</section>"
            )
        if not rendered:
            put_html(
                '<div class="event-inline-empty">Этапы фарма пока не отображаются.</div>'
            )

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
        currency_markup = self._event_currency_markup(
            plan, css_class="event-general-v2-currency-icon"
        )
        current_value = self._fmt(current_pt) if current_pt is not None else "Нет данных"
        target_value = self._fmt(planning_target) if planning_target else "Не задана"
        remaining_value = self._fmt(remaining) if remaining is not None else "—"
        current_html = (
            f'{currency_markup}<span>{escape(current_value)}</span>'
            if current_pt is not None
            else escape(current_value)
        )
        target_html = (
            f'{currency_markup}<span>{escape(target_value)}</span>'
            if planning_target
            else escape(target_value)
        )
        remaining_html = (
            f'{currency_markup}<span>{escape(remaining_value)}</span>'
            if remaining is not None
            else escape(remaining_value)
        )

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
    <div class="event-general-v2-metric event-general-v2-metric-accent"><small>Текущий баланс</small><strong class="event-currency-value">{current_html}</strong><span>Обновляется автоматически</span></div>
    <div class="event-general-v2-metric"><small>Цель фарма</small><strong class="event-currency-value">{target_html}</strong><span>Настраивается вручную</span></div>
    <div class="event-general-v2-metric"><small>Осталось набрать</small><strong class="event-currency-value">{remaining_html}</strong><span>До текущей цели</span></div>
    <div class="event-general-v2-metric"><small>Прогресс</small><strong>{str(progress) + "%" if current_pt is not None and planning_target else "—"}</strong><span>По текущему балансу</span></div>
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

    @staticmethod
    def _event_general_scope_layout() -> tuple[tuple[str, str], tuple[str, str]]:
        """Единый контракт: двухколоночный shell и длинные секции в main-column."""
        return (
            ("group_EventMainColumn", "group_EventSideColumn"),
            ("group_EventSources", "group_EventStages"),
        )

    def _render_event_general_v2(
        self,
        *,
        config: Mapping[str, Any],
        group_map: Mapping[str, Any],
    ) -> None:
        plan = self._event_plan()
        top_scopes, main_scopes = self._event_general_scope_layout()
        with use_scope("groups"):
            put_row(
                [put_scope(name) for name in top_scopes],
                size="minmax(0, 1fr) minmax(330px, 360px)",
            ).style("--event-general-v2-layout--")

        with use_scope("group_EventMainColumn"):
            put_scope("group_EventOverview")
            for name in main_scopes:
                put_scope(name)
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
            "EventGeneral", "TaskBalancer", group_map, config, False
        )
        with use_scope("group_EventSources", clear=True):
            self._render_event_sources_v2(plan)
        with use_scope("group_EventStages", clear=True):
            self._render_event_stages_v2(plan=plan, remaining_pt=remaining_pt)
