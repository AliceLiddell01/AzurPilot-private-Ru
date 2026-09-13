from __future__ import annotations

import types
from unittest.mock import Mock, patch

import pytest

from module.handler.login import LoginHandler, LoginHandlerTimeoutError


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


def test_login_flow_timeout_is_bounded_and_restores_screenshot_interval():
    handler = LoginHandler.__new__(LoginHandler)
    handler.device = types.SimpleNamespace(
        screenshot_interval_set=Mock(),
        stuck_record_clear=Mock(),
        click_record_clear=Mock(),
        screenshot=Mock(),
    )

    with pytest.raises(LoginHandlerTimeoutError):
        handler.handle_app_login(timeout_seconds=0.0)

    assert handler.device.screenshot_interval_set.call_args_list == [
        ((1.0,), {}),
        ((), {}),
    ]
    handler.device.screenshot.assert_not_called()


@pytest.mark.parametrize(
    "timeout_seconds", [float("nan"), float("inf"), float("-inf")]
)
def test_login_flow_rejects_non_finite_timeout(timeout_seconds: float):
    handler = LoginHandler.__new__(LoginHandler)

    with pytest.raises(ValueError, match="конечным"):
        handler._handle_app_login(timeout_seconds=timeout_seconds)
