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


def test_unknown_pt_sources_move_to_event_quests_only():
    mixin = EventGeneralV2Mixin()
    plan = {
        "pt_sources": [
            {"kind": "repeatable_map_clear", "name": "A1", "points": None},
            {"kind": "unknown", "name": "Clear B3", "points": 800},
            {"kind": "daily", "name": "Daily", "points": 300},
        ]
    }

    overview, quests = mixin._split_event_sources(plan)

    assert [item["name"] for item in overview] == ["A1", "Daily"]
    assert [item["name"] for item in quests] == ["Clear B3"]


def test_overview_does_not_render_rewards_and_rewards_page_owns_them():
    overview = inspect.getsource(EventGeneralV2Mixin._render_event_general_v2)
    rewards = inspect.getsource(EventGeneralV2Mixin._render_event_rewards_v2)

    assert "milestones" not in overview
    assert "event-reward-track" not in overview
    assert "milestones" in rewards
    assert "event-reward-track" in rewards
    assert "Задания события" in rewards


def test_overview_orders_profiles_and_balance_before_sources_and_stages():
    source = inspect.getsource(EventGeneralV2Mixin._render_event_general_v2)
    profiles = source.index('put_scope("group_EventProfiles")')
    balance = source.index('"TaskBalancer"')
    sources = source.index('put_scope("group_EventSources")')
    stages = source.index('put_scope("group_EventStages")')

    assert profiles < balance < sources < stages


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
    ):
        assert technical_text not in source


def test_reward_track_is_horizontal_scroll_snap_carousel():
    css = CSS.read_text(encoding="utf-8")
    assert ".event-reward-track" in css
    assert "grid-auto-flow: column" in css
    assert "overflow-x: auto" in css
    assert "scroll-snap-type: inline mandatory" in css
    assert ".event-reward-card-next" in css


def test_profiles_and_task_balance_share_compact_surface_contract():
    css = CSS.read_text(encoding="utf-8")
    selector = (
        '#pywebio-scope-content.event-modern-page[data-event-task="EventGeneral"] '
        '#pywebio-scope-group_EventProfiles,\n'
        '#pywebio-scope-content.event-modern-page[data-event-task="EventGeneral"] '
        '#pywebio-scope-group_TaskBalancer'
    )
    assert selector in css
    assert "padding: 14px !important" in css
    assert "border-radius: 12px !important" in css


def test_event_general_v2_styles_are_loaded_before_content_render():
    app = APP.read_text(encoding="utf-8")
    assert "from module.webui.app_event_general_v2 import EventGeneralV2Mixin" in app
    assert 'add_css(filepath_css("event-general-v2-alas"))' in app
