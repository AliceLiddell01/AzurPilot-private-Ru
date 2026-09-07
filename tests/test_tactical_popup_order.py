from __future__ import annotations

from unittest.mock import Mock

from module.tactical.tactical_class import RewardTacticalClass


def test_tactical_popup_is_handled_before_reward_navigation(monkeypatch):
    handler = object.__new__(RewardTacticalClass)
    calls = []

    monkeypatch.setattr(handler, "appear_then_click", lambda *args, **kwargs: False)
    monkeypatch.setattr(handler, "handle_popup_confirm", lambda *args, **kwargs: False)
    monkeypatch.setattr(handler, "handle_urgent_commission", lambda: False)
    monkeypatch.setattr(
        handler,
        "ui_page_main_popups",
        lambda: calls.append("main_popups") or True,
    )
    monkeypatch.setattr(handler, "interval_reset", Mock())
    navigation = Mock(return_value=False)
    monkeypatch.setattr(handler, "ui_main_appear_then_click", navigation)

    assert handler._handle_tactical_popups() == (True, False)
    assert calls == ["main_popups"]
    navigation.assert_not_called()
