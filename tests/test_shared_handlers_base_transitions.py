from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from module.handler.ambush import AmbushHandler
from module.handler.auto_search import AUTO_SEARCH_SETTINGS, AutoSearchHandler
from module.handler.enemy_searching import EnemySearchingHandler
from module.handler.fast_forward import AUTO_SEARCH, FastForwardHandler
from module.handler.info_handler import POPUP_SINGLE_WHITE, InfoHandler
from module.handler.login import LoginHandler
from module.handler.mystery import MysteryHandler
from module.handler.strategy import StrategyHandler


def bare(cls):
    return object.__new__(cls)


def test_ambush_disabled_keeps_early_return_without_detection_or_clicks():
    handler = bare(AmbushHandler)
    handler.config = SimpleNamespace(MAP_HAS_AMBUSH=False)
    handler._air_raid_appear = Mock()
    handler._ambush_appear = Mock()

    assert handler.handle_ambush() is False
    handler._air_raid_appear.assert_not_called()
    handler._ambush_appear.assert_not_called()


def test_enemy_searching_outside_map_keeps_false_without_screenshot():
    handler = bare(EnemySearchingHandler)
    handler.is_in_map = Mock(return_value=False)
    handler.device = SimpleNamespace(screenshot=Mock())

    assert handler.handle_in_map_no_enemy_searching() is False
    handler.device.screenshot.assert_not_called()


def test_auto_search_already_active_keeps_early_return_without_click():
    handler = bare(AutoSearchHandler)
    handler.image_color_count = Mock(side_effect=[True, False, False, False, False, False])
    handler.device = SimpleNamespace(click=Mock())

    assert handler._auto_search_set_click("fleet1_mob_fleet2_boss") is True
    assert handler.image_color_count.call_count == len(AUTO_SEARCH_SETTINGS)
    handler.device.click.assert_not_called()


def test_fast_forward_unknown_switch_keeps_false_without_setting(monkeypatch):
    handler = bare(FastForwardHandler)
    handler.config = SimpleNamespace(Campaign_UseAutoSearch=False)
    handler.map_is_auto_search = False
    handler._auto_search_set = Mock()
    monkeypatch.setattr(AUTO_SEARCH, "get", lambda main: "unknown")

    assert handler.handle_auto_search() is False
    handler._auto_search_set.assert_not_called()


def test_info_single_white_keeps_same_click_target_and_return():
    handler = bare(InfoHandler)
    handler.appear_then_click = Mock(return_value=True)

    assert handler.handle_popup_single_white(interval=7) is True
    handler.appear_then_click.assert_called_once_with(
        POPUP_SINGLE_WHITE,
        offset=(20, 20),
        interval=7,
    )


def test_login_restart_success_first_try_keeps_stop_start_and_wait_sequence():
    handler = bare(LoginHandler)
    handler.config = SimpleNamespace(Restart_ClearCache=False)
    handler.device = SimpleNamespace(
        app_stop=Mock(),
        app_clear=Mock(),
        sleep=Mock(),
        app_start=Mock(),
        app_is_running=Mock(return_value=True),
    )
    handler.handle_app_login = Mock()

    handler.app_restart()

    handler.device.app_stop.assert_called_once_with()
    handler.device.app_clear.assert_not_called()
    handler.device.app_start.assert_called_once_with()
    assert handler.device.sleep.call_args_list[0].args == (3,)
    assert handler.device.sleep.call_args_list[1].args == (30,)
    handler.device.app_is_running.assert_called_once_with()
    handler.handle_app_login.assert_called_once_with()


def test_mystery_no_item_keeps_false_without_click():
    handler = bare(MysteryHandler)
    handler.config = SimpleNamespace(MAP_MYSTERY_MAP_CLICK=True)
    handler.appear = Mock(return_value=False)
    handler.device = SimpleNamespace(click=Mock())

    assert handler.handle_mystery_items(button=None, drop=None) is False
    handler.device.click.assert_not_called()


def test_strategy_fixed_formation_keeps_early_return_without_opening_panel():
    handler = bare(StrategyHandler)
    handler.fleet_1_formation_fixed = True
    handler.strategy_open = Mock()

    assert handler.handle_strategy(1) is False
    handler.strategy_open.assert_not_called()
