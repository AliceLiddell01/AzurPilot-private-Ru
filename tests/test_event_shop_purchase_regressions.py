import inspect
from types import SimpleNamespace

import module.webui.event_shop_priority as priority
from module.shop_event.clerk import EventShopClerk
from module.shop_event.shop_event import EventShop
from module.webui.app_event_shop_v2 import EventShopV2Mixin


def test_partial_quantity_uses_shorter_click_path():
    assert EventShopClerk._prefer_amount_max(1, 10, 100) is False
    assert EventShopClerk._prefer_amount_max(1, 90, 100) is True
    assert EventShopClerk._prefer_amount_max(10, 10, 100) is False


def test_event_shop_refreshes_pt_before_complete_scan():
    source = inspect.getsource(EventShop._run)

    assert source.index("self.get_current_pts()") < source.index("items = self.scan_all()")


def test_event_shop_pt_refresh_updates_journal_and_runtime_observation():
    source = inspect.getsource(EventShop.get_current_pts)

    assert "LogRes(config=self.config).Pt = self.pt" in source
    assert 'source="event_shop_ocr"' in source


def test_completed_goal_clear_preserves_newer_user_edit(monkeypatch):
    user_state = {
        "source_event_id": "event-test",
        "shop_selections": {"11": 10},
    }
    saved_states = []
    monkeypatch.setattr(
        priority,
        "load_event_user_state",
        lambda _instance: user_state,
    )
    monkeypatch.setattr(
        priority,
        "save_event_user_state",
        lambda _instance, state: saved_states.append(state),
    )
    config = SimpleNamespace(config_name="test-instance")

    assert priority._clear_selected_target(config, "event-test", "11", 10) is True
    assert saved_states[-1]["shop_selections"]["11"] == 0

    saved_states.clear()
    user_state["shop_selections"]["11"] = 11
    assert priority._clear_selected_target(config, "event-test", "11", 10) is False
    assert saved_states == []


def test_event_shop_card_keeps_availability_next_to_terminal_status():
    source = inspect.getsource(EventShopV2Mixin._render_event_shop_priority_plan)

    assert 'priority_state.get("completed")' in source
    assert "Полностью куплено" in source
    assert 'event-shop-v2-stock">Доступно: {available}' in source
    assert "state_html = status_html + availability_html" in source
