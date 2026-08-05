"""Regression test for the shifted EN Operation Siren Storage button."""

from types import SimpleNamespace

from module.os.tasks.voucher import (
    DATA_LOGGER_STORAGE_ENTER_SECONDS,
    OpsiVoucher,
)
from module.os_handler.assets import AUTO_SEARCH_REWARD, MISSION_CHECK, STORAGE_ENTER


class FrameTimer:
    def __init__(self):
        self.calls = 0

    def start(self):
        return self

    def reached(self):
        self.calls += 1
        return self.calls > 3


class StorageEntryHarness(OpsiVoucher):
    def __init__(self):
        self.storage_clicked = False
        self.info_bar_calls = 0
        self.map_event_calls = 0
        self.interval_clears = []
        self.zone = SimpleNamespace(is_azur_port=True)
        self.device = SimpleNamespace(
            screenshot=lambda: None,
            click=lambda _button: None,
        )

    def _data_logger_ensure_port_map(self):
        return True

    def interval_clear(self, button):
        self.interval_clears.append(button)

    def is_in_map(self):
        return True

    def is_in_storage(self):
        return self.storage_clicked

    def handle_info_bar(self):
        self.info_bar_calls += 1

    def appear(self, button, **_kwargs):
        assert button is MISSION_CHECK
        return False

    def appear_then_click(self, button, **kwargs):
        if button is STORAGE_ENTER:
            assert kwargs == {
                'offset': (200, 5),
                'interval': 3,
            }
            self.storage_clicked = True
            return True
        assert button is AUTO_SEARCH_REWARD
        return False

    def handle_map_event(self):
        self.map_event_calls += 1
        return False


def test_storage_entry_accepts_shifted_en_button_position(monkeypatch):
    timer = FrameTimer()
    monkeypatch.setattr(
        'module.os.tasks.voucher.Timer.from_seconds',
        lambda seconds: timer
        if seconds == DATA_LOGGER_STORAGE_ENTER_SECONDS
        else None,
    )
    task = StorageEntryHarness()

    assert task._data_logger_storage_enter() is True
    assert task.storage_clicked is True
    assert task.info_bar_calls == 1
    assert task.interval_clears == [STORAGE_ENTER]
