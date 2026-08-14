import inspect
from pathlib import Path

from module.webui.app import AlasGUI
from module.webui.app_event_general_v2 import EVENT_REWARDS_TASK, EventGeneralV2Mixin
from module.webui.app_event_profiles import EventProfilesMixin
from module.webui.event_profiles import (
    EVENT_TASK_LABELS,
    event_task_label,
    validate_event_profile_name,
)


ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "module" / "config" / "argument" / "task.yaml"
CSS = ROOT / "assets" / "gui" / "css" / "event-general-v2-alas.css"
POLISH_CSS = ROOT / "assets" / "gui" / "css" / "event-general-v2-polish-alas.css"
SOURCE = ROOT / "module" / "webui" / "app_event_general_v2.py"
APP = ROOT / "module" / "webui" / "app.py"


def test_event_general_v2_wraps_profile_and_legacy_event_layers():
    mro = AlasGUI.__mro__
    v2 = mro.index(EventGeneralV2Mixin)
    profiles = mro.index(EventProfilesMixin)
    assert v2 < profiles


def test_rewards_page_is_webui_only_and_does_not_create_runtime_task():
    text = TASKS.read_text(encoding="utf-8")
    assert "EventRewards:" not in text

    mixin = EventGeneralV2Mixin()
    mixin.ALAS_MENU = {
        "Event": {
            "menu": "collapse",
            "page": "setting",
            "tasks": ["EventGeneral", "Event", "EventShop", "WarArchives"],
        }
    }
    original = mixin.ALAS_MENU
    original_tasks = list(original["Event"]["tasks"])

    mixin._ensure_event_rewards_menu_entry()

    assert mixin.ALAS_MENU is not original
    assert original["Event"]["tasks"] == original_tasks
    assert mixin.ALAS_MENU["Event"]["tasks"] == [
        "EventGeneral",
        "Event",
        "EventShop",
        EVENT_REWARDS_TASK,
        "WarArchives",
    ]


def test_event_menu_uses_user_facing_overview_and_rewards_labels():
    assert EVENT_TASK_LABELS["EventGeneral"] == "Общая информация о текущем ивенте"
    assert EVENT_TASK_LABELS[EVENT_REWARDS_TASK] == "Награды ивента"
    assert event_task_label({}, "EventGeneral", "fallback") == EVENT_TASK_LABELS["EventGeneral"]
    assert event_task_label({}, EVENT_REWARDS_TASK, "fallback") == "Награды ивента"
    assert validate_event_profile_name({}, "Награды ивента") is not None


def test_quest_sources_move_off_overview_and_keep_map_sources():
    mixin = EventGeneralV2Mixin()
    plan = {
        "pt_sources": [
            {"kind": "repeatable_map_clear", "name": "A1", "points": None},
            {"kind": "unknown", "name": "Clear B3", "points": 800},
            {"kind": "daily", "name": "Daily", "points": 300},
            {"kind": "one_time", "name": "One time", "points": 500},
            {"kind": "challenge", "name": "SP", "points": 1000},
        ]
    }

    overview, quests = mixin._split_event_sources(plan)

    assert [item["name"] for item in overview] == ["A1", "SP"]
    assert [item["name"] for item in quests] == ["Clear B3", "Daily", "One time"]


def test_map_grouping_separates_a_b_c_d_and_special_stages():
    mixin = EventGeneralV2Mixin()
    items = [
        {"name": "A1"},
        {"name": "A2"},
        {"name": "B1"},
        {"name": "C3"},
        {"name": "D2"},
        {"name": "SP"},
        {"name": "EXTRA"},
    ]

    groups = mixin._group_map_items(items)

    assert [(title, subtitle, [item["name"] for item in rows]) for title, subtitle, rows in groups] == [
        ("Карта A", "Нормальная сложность", ["A1", "A2"]),
        ("Карта B", "Нормальная сложность", ["B1"]),
        ("Карта C", "Hard-сложность", ["C3"]),
        ("Карта D", "Hard-сложность", ["D2"]),
        ("Особые этапы", "SP и EXTRA", ["SP", "EXTRA"]),
    ]


def test_current_quest_templates_get_russian_titles_and_daily_event_groups():
    mixin = EventGeneralV2Mixin()

    assert mixin._quest_presentation({"kind": "unknown", "name": "Build 3 ships."})[:3] == (
        "daily",
        "Построить 3 корабля",
        "Постройте 3 корабля на верфи.",
    )
    assert mixin._quest_presentation(
        {"kind": "unknown", "name": "Sortie and obtain 15 victories."}
    )[:2] == ("daily", "Одержать 15 побед")
    assert mixin._quest_presentation(
        {"kind": "unknown", "name": "Clear any Hard Mode stage 1 time."}
    )[:2] == ("daily", "Пройти этап в режиме Hard")
    assert mixin._quest_presentation(
        {"kind": "unknown", "name": "Clear A1 or C1"}
    )[:2] == ("event", "Пройти A1 или C1")
    assert mixin._quest_presentation(
        {"kind": "unknown", "name": "Clear any event stage 60 times."}
    )[:2] == ("event", "Пройти любой этап события 60 раз")


def test_unknown_quest_fallback_keeps_original_text_instead_of_inventing_translation():
    mixin = EventGeneralV2Mixin()
    group, title, description, original = mixin._quest_presentation(
        {"kind": "unknown", "name": "Unexpected source phrase"}
    )

    assert group == "event"
    assert title == "Задание события"
    assert "указанное в источнике" in description
    assert original == "Unexpected source phrase"


def test_overview_does_not_render_rewards_and_rewards_page_owns_them():
    overview = inspect.getsource(EventGeneralV2Mixin._render_event_general_v2)
    rewards = inspect.getsource(EventGeneralV2Mixin._render_event_rewards_v2)

    assert "milestones" not in overview
    assert "event-reward-track" not in overview
    assert "milestones" in rewards
    assert "event-reward-track" in rewards
    assert "Ежедневные задания" in rewards
    assert "Задания события" in rewards


def test_overview_uses_eventshop_style_main_and_right_rail_composition():
    source = inspect.getsource(EventGeneralV2Mixin._render_event_general_v2)

    assert 'put_scope("group_EventMainColumn")' in source
    assert 'put_scope("group_EventSideColumn")' in source
    assert 'size="minmax(0, 1fr) minmax(330px, 360px)"' in source
    assert source.index('put_scope("group_EventProfiles")') < source.index(
        'put_scope("group_TaskBalancer")'
    )
    assert source.index('put_scope("group_EventSources")') < source.index(
        'put_scope("group_EventStages")'
    )


def test_profile_partial_refresh_keeps_compact_renderer():
    source = inspect.getsource(EventGeneralV2Mixin._refresh_event_profile_ui)
    assert "_render_event_profiles_compact" in source
    assert "_render_event_profile_manager" not in source
    assert 'active_button("menu", "EventGeneral")' in source


def test_user_facing_event_general_renderer_hides_runtime_and_source_internals():
    source = SOURCE.read_text(encoding="utf-8")
    for technical_text in (
        "Runtime eligible",
        "Runtime blocked",
        "verified",
        "source_status",
        "revision",
        "Repository",
        "datamine",
        "event-map-identity",
        "51101",
    ):
        assert technical_text not in source


def test_reward_track_is_horizontal_scroll_snap_carousel():
    css = CSS.read_text(encoding="utf-8")
    polish = POLISH_CSS.read_text(encoding="utf-8")

    assert ".event-reward-track" in css
    assert "grid-auto-flow: column" in css
    assert "overflow-x: auto" in css
    assert "scroll-snap-type: inline mandatory" in css
    assert ".event-reward-card-next" in css
    assert "grid-auto-columns: minmax(270px, 300px)" in polish
    assert "min-height: 242px" in polish


def test_reward_and_content_cards_use_stronger_surfaces_without_fading_reached_cards():
    polish = POLISH_CSS.read_text(encoding="utf-8")

    assert ".event-reward-track-card" in polish
    assert ".event-source-card-v2" in polish
    assert ".event-farm-card-v2" in polish
    assert ".event-quest-card" in polish
    assert "background: var(--event-surface-strong) !important" in polish
    assert ".event-reward-track-card.event-reward-card-reached" in polish
    assert "opacity: 1 !important" in polish


def test_quest_cards_use_currency_icon_and_do_not_render_completion_state():
    source = inspect.getsource(EventGeneralV2Mixin._render_event_quest_group)

    assert "event-quest-reward" in source
    assert "currency_icon" in source
    assert "<img" in source
    assert "Выполнено" not in source
    assert "completed" not in source
    assert ">PT<" not in source


def test_profiles_and_task_balance_share_compact_surface_contract():
    css = CSS.read_text(encoding="utf-8")
    polish = POLISH_CSS.read_text(encoding="utf-8")
    selector = (
        '#pywebio-scope-content.event-modern-page[data-event-task="EventGeneral"] '
        '#pywebio-scope-group_EventProfiles,\n'
        '#pywebio-scope-content.event-modern-page[data-event-task="EventGeneral"] '
        '#pywebio-scope-group_TaskBalancer'
    )
    assert selector in css
    assert "padding: 14px !important" in css
    assert "border-radius: 12px !important" in css
    assert "#pywebio-scope-group_EventSideColumn" in polish
    assert "position: sticky" in polish


def test_event_general_v2_styles_are_loaded_before_content_render():
    app = APP.read_text(encoding="utf-8")
    assert "from module.webui.app_event_general_v2 import EventGeneralV2Mixin" in app
    assert 'add_css(filepath_css("event-general-v2-alas"))' in app
    assert 'add_css(filepath_css("event-general-v2-polish-alas"))' in app
