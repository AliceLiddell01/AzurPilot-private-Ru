from pathlib import Path

import yaml

from module.webui.app import AlasGUI
from module.webui.app_event_datamine import EventDatamineMixin
from module.webui.app_event_general_presentation import EventGeneralPresentationMixin
from module.webui.app_event_layout import EventLayoutMixin
from module.webui.app_event_profiles import EventProfilesMixin
from module.webui.app_event_shop_safety import EventShopSafetyMixin
from module.webui.app_task_config import TaskConfigMixin

ROOT = Path(__file__).resolve().parents[1]
LAYOUT = ROOT / "module" / "webui" / "app_event_layout.py"
PRESENTATION = ROOT / "module" / "webui" / "app_event_general_presentation.py"
PLANNER = ROOT / "module" / "webui" / "app_event_planner.py"
SHOP_SAFETY = ROOT / "module" / "webui" / "app_event_shop_safety.py"
TASKS = ROOT / "module" / "config" / "argument" / "task.yaml"
EVENT_CSS = ROOT / "assets" / "gui" / "css" / "event-profiles-alas.css"


def test_event_layout_is_inserted_before_generic_task_renderer():
    mro = AlasGUI.__mro__
    presentation = mro.index(EventGeneralPresentationMixin)
    profiles = mro.index(EventProfilesMixin)
    datamine = mro.index(EventDatamineMixin)
    safety = mro.index(EventShopSafetyMixin)
    layout = mro.index(EventLayoutMixin)
    generic = mro.index(TaskConfigMixin)
    assert presentation < profiles < datamine < safety < layout < generic


def test_datamine_source_does_not_bypass_shop_safety_write_wrapper():
    assert "_event_plan_write" not in EventDatamineMixin.__dict__
    assert AlasGUI._event_plan_write is EventShopSafetyMixin._event_plan_write


def test_event_pages_mark_only_event_content_for_modern_styles():
    source = LAYOUT.read_text(encoding="utf-8")
    assert '@use_scope("content", clear=True)\n    def _alas_set_event_group' in source
    assert 'content.classList.add("event-modern-page")' in source
    assert 'document.body.classList.add("event-modern-active")' in source
    assert 'content.classList.remove("event-modern-page")' in source
    assert "if task not in EVENT_LAYOUT_TASKS:" in source
    assert "self._unmark_event_page()" in source
    assert "return super().alas_set_group(task)" in source


def test_event_map_progressive_disclosure_contract():
    source = LAYOUT.read_text(encoding="utf-8")
    for group in ("Scheduler", "Campaign", "StopCondition", "Fleet", "Emotion"):
        assert f'"{group}"' in source
    for group in ("Submarine", "HpControl", "EnemyPriority"):
        assert f'"{group}"' in source
    assert 'title="Расширенные настройки карты"' in source
    assert "put_collapse(" in source
    assert "event-map-intro" in source


def test_advanced_groups_render_directly_without_legacy_dom_reparent():
    source = LAYOUT.read_text(encoding="utf-8")
    assert '*[put_scope(f"group_{name}") for name in existing]' not in source
    assert "body.appendChild(node)" not in source
    assert "document.createElement(\"details\")" not in source
    assert "with use_scope(body_scope, clear=True):" in source
    assert "self._render_named_group(task, name, group_map, config, False)" in source


def test_event_general_uses_one_explicit_target_action():
    layout = LAYOUT.read_text(encoding="utf-8")
    presentation = PRESENTATION.read_text(encoding="utf-8")
    combined = layout + presentation
    assert 'put_scope("group_EventStop")' not in combined
    assert '"Настроить цель фарма"' in combined

    obsolete_actions = (
        "Изменить целевой PT",
        "Взять PT из плана магазина",
        "Записать окончание фарма из плана",
        "Отключить ограничение по времени",
        "Взять цель из магазина",
    )
    for label in obsolete_actions:
        assert label not in combined


def test_event_general_dashboard_uses_canonical_local_plan_projection():
    presentation = PRESENTATION.read_text(encoding="utf-8")
    planner = PLANNER.read_text(encoding="utf-8")

    assert '"group_EventMainColumn"' in presentation
    assert '"group_EventSideColumn"' in presentation
    assert '"group_EventSources"' in presentation
    assert '"group_EventStages"' in presentation
    assert "event-general-v2-hero" in presentation
    assert "event-general-v2-metrics" in presentation
    assert "event-general-v2-progress" in presentation
    assert "planning_target = max(target, shop_total)" in presentation
    assert "remaining_pt" in presentation
    assert '"Получено"' not in presentation
    assert '"Пропуск"' not in presentation
    assert '"Добавить источник PT"' not in presentation
    assert '"Добавить этап"' not in presentation
    assert "BWiki" not in presentation

    datamine = (ROOT / "module/webui/app_event_datamine.py").read_text(encoding="utf-8")
    assert 'deep_get(config, "Dashboard.Pt.Value", None)' in datamine
    assert 'deep_get(config, "Dashboard.Pt.Record", "")' in datamine
    assert "dashboard_pt_observation" in datamine
    assert "load_event_plan_from_artifact" in datamine
    assert "load_current_event_plan" not in datamine
    assert '"manual"' not in planner


def test_event_shop_has_one_primary_action_and_auto_syncs_fail_closed():
    layout = LAYOUT.read_text(encoding="utf-8")
    safety = SHOP_SAFETY.read_text(encoding="utf-8")

    assert 'put_scope("group_EventShopPlan")' in layout
    assert "event-shop-hero" in layout
    assert '"Добавить товар"' not in layout
    assert 'title="Расширенные настройки — автоматизация магазина"' in layout
    assert layout.index('put_scope("group_EventShopPlan")') < layout.index(
        'self._render_named_group(task, "Scheduler", group_map, config)'
    )

    for label in (
        "Выбрать всё",
        "Очистить выбор",
        "Только записать целевой PT",
        "Синхронизировать с EventShop",
    ):
        assert label not in layout

    assert "def _event_plan_write" in safety
    assert "self._sync_shop_plan_fail_closed(plan, announce=False)" in safety
    assert '"EventShop.Scheduler.Enable": bool(enabled)' in safety
    assert '"EventShop.EventShop.PresetFilter": "custom"' in safety
    assert '"EventShop.EventShop.CustomFilter": compiled.filter_text' in safety
    assert '"EventShop.EventShop.UnlockSSRShip": False' in safety
    assert '"EventShop.EventShop.BuyURShip": 0' in safety
    assert '"EventGeneral.EventGeneral.PtLimit": total' not in safety
    assert '"EventGeneral.EventGeneral.PtLimit": pt_limit' not in safety
    assert "PT-автостоп не изменён" in safety
    assert "event-automation-status" in safety


def test_event_shop_invalid_or_empty_plan_pauses_scheduler():
    safety = SHOP_SAFETY.read_text(encoding="utf-8")
    assert "if total <= 0:" in safety
    assert "problem = self._compiled_shop_problem(compiled)" in safety
    assert "self._set_event_shop_scheduler(False)" in safety
    assert "старый фильтр не продолжал покупки" in safety
    assert "Автоматизация магазина приостановлена" in safety


def test_event_css_defines_modern_responsive_visual_system():
    css = EVENT_CSS.read_text(encoding="utf-8")
    for selector in (
        ".event-dashboard-hero",
        ".event-metrics-grid",
        ".event-metric-card",
        ".event-progress-track",
        ".event-shop-hero",
        ".event-shop-grid",
        ".event-shop-card",
        ".event-automation-status",
        'details[style*="--event-advanced-details--"]',
    ):
        assert selector in css
    assert ".event-advanced-details" not in css
    assert ".event-advanced-body" not in css
    assert ".event-details-chevron" not in css
    assert '[id^="pywebio-scope-event_advanced_"]' in css
    assert "var(--alas-entry-surface" in css
    assert "var(--alas-entry-accent" in css
    assert "var(--alas-apple-card-bg" in css
    assert "border-radius" in css
    assert ".event-dashboard-hero::after" not in css
    assert "radial-gradient" not in css
    assert "width: min(100%, 1120px)" not in css
    assert "grid-template-columns: repeat(auto-fit, minmax(250px, 1fr))" in css
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in css
    assert "grid-template-columns: repeat(auto-fit, minmax(220px, 1fr))" in css
    assert "@media (max-width: 760px)" in css
    assert "@media (prefers-reduced-motion: reduce)" in css


def test_stage_two_does_not_remove_runtime_event_groups():
    tasks = yaml.safe_load(TASKS.read_text(encoding="utf-8"))["Event"]["tasks"]

    assert tasks["EventGeneral"] == ["EventGeneral", "TaskBalancer"]
