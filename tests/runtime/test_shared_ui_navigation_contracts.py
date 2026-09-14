from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from module.exception import ScriptError
from module.ui.navbar import Navbar
from module.ui.page import Page, page_campaign, page_campaign_menu, page_main, page_main_white
from module.ui.scroll import Scroll
from module.ui.setting import Setting
from module.ui.switch import Switch
from module.ui.ui import UI


def bare_ui() -> UI:
    return object.__new__(UI)


def test_page_graph_keeps_campaign_parent_and_click_target():
    Page.init_connection(page_campaign)
    try:
        assert page_main.parent is page_campaign_menu
        assert page_main.parent in page_main.links
        assert page_main.links[page_main.parent] is not None
    finally:
        Page.clear_connection()


def test_ui_ensure_same_page_returns_false_without_navigation():
    ui = bare_ui()
    ui.ui_current = page_main
    ui.ui_get_current_page = Mock()
    ui.ui_goto = Mock()

    assert ui.ui_ensure(page_main) is False
    ui.ui_get_current_page.assert_called_once_with(skip_first_screenshot=True)
    ui.ui_goto.assert_not_called()


def test_ui_ensure_equivalent_main_theme_returns_false_without_navigation():
    ui = bare_ui()
    ui.ui_current = page_main_white
    ui.ui_get_current_page = Mock()
    ui.ui_goto = Mock()

    assert ui.ui_ensure(page_main) is False
    ui.ui_goto.assert_not_called()


def test_ui_ensure_different_page_navigates_and_returns_true():
    ui = bare_ui()
    ui.ui_current = page_main
    ui.ui_get_current_page = Mock()
    ui.ui_goto = Mock()

    assert ui.ui_ensure(page_campaign) is True
    ui.ui_goto.assert_called_once_with(page_campaign, skip_first_screenshot=True)


def test_ui_additional_preserves_os_popup_priority():
    ui = bare_ui()
    ui.ui_page_os_popups = Mock(return_value=True)
    ui.handle_popup_confirm = Mock()
    ui.handle_urgent_commission = Mock()
    ui.ui_page_main_popups = Mock()

    assert ui.ui_additional() is True
    ui.ui_page_os_popups.assert_called_once_with()
    ui.handle_popup_confirm.assert_not_called()
    ui.handle_urgent_commission.assert_not_called()
    ui.ui_page_main_popups.assert_not_called()


def test_navbar_invalid_direction_returns_false_without_device_access():
    navbar = Navbar(SimpleNamespace(_name='TEST_NAVBAR', buttons=[]))
    main = Mock()

    assert navbar.set(main) is False
    main.device.assert_not_called()


def test_scroll_drag_page_preserves_target_calculation():
    scroll = Scroll((0, 0, 10, 100), color=(255, 255, 255), name='TEST_SCROLL')
    scroll.length = 20
    scroll.cal_position = Mock(return_value=0.25)
    scroll.set = Mock(return_value=3)
    main = Mock()

    assert scroll.drag_page(0.8, main=main) == 3
    scroll.set.assert_called_once_with(
        0.45,
        main=main,
        random_range=(-0.05, 0.05),
        skip_first_screenshot=True,
    )


def test_setting_selection_preserves_option_mapping():
    setting = Setting()
    rarity_button = object()
    level_button = object()
    setting.settings = {
        ('sort', 'rarity'): rarity_button,
        ('sort', 'level'): level_button,
    }
    setting.settings_default = {'sort': 'rarity'}

    status = setting._product_setting_status(sort='level')

    assert status == {
        rarity_button: False,
        level_button: True,
    }


def test_setting_invalid_default_keeps_script_error():
    setting = Setting()

    with pytest.raises(ScriptError):
        setting.add_setting(
            'sort',
            option_buttons=[],
            option_names=['rarity'],
            option_default='level',
        )


def test_switch_invalid_state_keeps_script_error():
    switch = Switch('TEST_SWITCH')

    with pytest.raises(ScriptError):
        switch.get_data('missing')


def test_switch_wait_timeout_returns_false_without_device_click():
    switch = Switch('TEST_SWITCH')
    timeout = Mock()
    timeout.reached.return_value = True
    switch.wait_timeout = Mock()
    switch.wait_timeout.reset.return_value = timeout
    switch.get = Mock(return_value='unknown')
    switch.handle_additional = Mock()
    main = SimpleNamespace(device=SimpleNamespace(screenshot=Mock(), click=Mock()))

    assert switch.wait(main) is False
    main.device.click.assert_not_called()
    switch.handle_additional.assert_not_called()
