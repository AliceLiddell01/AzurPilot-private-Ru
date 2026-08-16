from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from module.retire.assets import DOCK_CHECK, IN_RETIREMENT_CHECK, RETIRE_APPEAR_1, RETIRE_APPEAR_3
from module.retire.retirement import Retirement


class _ResetStub:
    def reset(self):
        return None


def _make_handler(mode: str = "one_click_retire") -> Retirement:
    handler = object.__new__(Retirement)
    handler.config = SimpleNamespace(Retirement_RetireMode=mode)
    handler._unable_to_enhance = False
    handler._retirement_game_tips_pending = False
    handler.map_cat_attack_timer = _ResetStub()
    handler.handle_game_tips = Mock(return_value=True)
    handler.appear = Mock(return_value=False)
    handler.appear_then_click = Mock(return_value=False)
    handler.interval_clear = Mock()
    handler.interval_reset = Mock()
    return handler


def test_retirement_does_not_consume_unrelated_game_tips():
    handler = _make_handler()

    assert handler.handle_retirement() is False
    handler.handle_game_tips.assert_not_called()


@pytest.mark.parametrize(
    ("mode", "trigger"),
    [
        ("one_click_retire", RETIRE_APPEAR_1),
        ("enhance", RETIRE_APPEAR_3),
    ],
)
def test_retirement_allows_game_tips_after_dock_full_transition(mode, trigger):
    handler = _make_handler(mode)
    handler.appear_then_click.side_effect = lambda button, **kwargs: button is trigger

    assert handler.handle_retirement() is False
    assert handler._retirement_game_tips_pending is True
    handler.handle_game_tips.assert_not_called()

    handler.appear_then_click = Mock(return_value=False)

    assert handler.handle_retirement() is True
    handler.handle_game_tips.assert_called_once_with()


def test_retirement_clears_game_tips_context_on_retirement_page():
    handler = _make_handler()
    handler._retirement_game_tips_pending = True
    handler.handle_game_tips = Mock(return_value=False)
    handler.appear.side_effect = lambda button, **kwargs: button is IN_RETIREMENT_CHECK
    handler._retire_handler = Mock()

    assert handler.handle_retirement() is True
    assert handler._retirement_game_tips_pending is False
    handler._retire_handler.assert_called_once_with()


def test_retirement_clears_game_tips_context_on_dock_page():
    handler = _make_handler("enhance")
    handler._retirement_game_tips_pending = True
    handler.handle_game_tips = Mock(return_value=False)
    handler.appear.side_effect = lambda button, **kwargs: button is DOCK_CHECK
    handler.handle_dock_cards_loading = Mock()
    handler._enhance_handler = Mock(return_value=(1, 5))

    assert handler.handle_retirement() is True
    assert handler._retirement_game_tips_pending is False
    handler.handle_dock_cards_loading.assert_called_once_with()
    handler._enhance_handler.assert_called_once_with()
