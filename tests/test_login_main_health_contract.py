from __future__ import annotations

import types
from unittest.mock import Mock, patch

from module.handler.login import LoginHandler


class _ImmediateTimer:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        return self

    def reached(self):
        return True

    def reset(self):
        return self


def test_login_popup_does_not_finish_flow_before_main_is_confirmed():
    handler = LoginHandler.__new__(LoginHandler)
    handler.config = types.SimpleNamespace(SERVER='en')
    handler.device = types.SimpleNamespace(
        stuck_record_clear=Mock(),
        click_record_clear=Mock(),
        get_orientation=Mock(),
        screenshot=Mock(),
        click=Mock(),
    )

    # Первый кадр — кнопка входа + GET_SHIP-подобный main popup.
    # Второй кадр — уже подтверждённый главный экран.
    handler.is_in_main = Mock(side_effect=[False, True])
    handler.match_template_color = Mock(return_value=True)
    handler.appear = Mock(return_value=False)
    handler.appear_then_click = Mock(return_value=False)
    handler.handle_cn_user_agreement = Mock(return_value=False)
    handler.handle_popup_confirm = Mock(return_value=False)
    handler.handle_urgent_commission = Mock(return_value=False)
    handler.ui_page_main_popups = Mock(return_value=True)

    with patch('module.handler.login.Timer', _ImmediateTimer):
        result = handler._handle_app_login()

    assert result is True
    assert handler.is_in_main.call_count == 2
    assert handler.device.screenshot.call_count >= 2
    handler.ui_page_main_popups.assert_called_once_with(get_ship=True)
