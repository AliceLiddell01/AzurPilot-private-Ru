from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from module.combat.assets import GET_ITEMS_1, GET_ITEMS_2
from module.config.deep import deep_get, deep_set
from module.config.opsi_data_logger import (
    DATA_LOGGER_MAX_FAILURES_PER_CYCLE,
    DATA_LOGGER_RETRY_COUNT_KEY,
    DATA_LOGGER_RETRY_PENDING_KEY,
    DATA_LOGGER_STORAGE_PATH,
    DataLoggerStorageState,
    data_logger_mark_active,
    data_logger_retry_count,
    data_logger_retry_pending,
    data_logger_set_retry,
)
from module.handler.assets import GET_MISSION
from module.os.tasks.voucher import (
    DATA_LOGGER_ACTIVATION_ABSENT_FRAMES,
    DATA_LOGGER_RETRY_MINUTES,
    DATA_LOGGER_STORAGE_USE_SECONDS,
    OpsiVoucher,
)
from module.os_handler.assets import GET_ADAPTABILITY, STORAGE_USE
from module.storage.assets import BOX_USE

FIXED_UTC_NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
NEXT_RESET = datetime(2026, 9, 1, 7, 0)


class FakeConfig:
    def __init__(self, *, intent: bool = True):
        self.data = {
            "OpsiExplore": {
                "OpsiExplore": {"SpecialRadar": intent},
                "Storage": {"Storage": {}},
            }
        }
        self.delays = []

    def cross_get(self, keys, default=None):
        return deep_get(self.data, keys=keys, default=default)

    def cross_set(self, keys, value):
        deep_set(self.data, keys=keys, value=value)

    def task_delay(self, *, target=None, minute=None, server_update=False):
        self.delays.append(
            {
                "target": target,
                "minute": minute,
                "server_update": server_update,
            }
        )


class FrameTimer:
    def __init__(self, frame_limit: int):
        self.frame_limit = frame_limit
        self.calls = 0

    def start(self):
        return self

    def reached(self):
        self.calls += 1
        return self.calls > self.frame_limit


class ActivationHarness(OpsiVoucher):
    def __init__(
        self,
        *,
        items_by_frame: dict[int, list[object]],
        use_frames: set[int] | None = None,
        success_frames: set[int] | None = None,
    ):
        self.frame = 0
        self.items_by_frame = items_by_frame
        self.use_frames = use_frames or set()
        self.success_frames = success_frames or set()
        self.clicked = []
        self.device = SimpleNamespace(
            screenshot=self._screenshot,
            click=self.clicked.append,
        )

    def _screenshot(self):
        self.frame += 1

    def interval_clear(self, _button):
        return None

    def appear(self, button, **_kwargs):
        if button is GET_MISSION:
            return False
        if button is GET_ADAPTABILITY:
            return False
        return False

    def appear_then_click(self, button, **_kwargs):
        if button is STORAGE_USE or button is BOX_USE:
            return self.frame in self.use_frames
        if button is GET_ITEMS_1 or button is GET_ITEMS_2:
            return self.frame in self.success_frames
        return False

    def handle_story_skip(self):
        return False

    def is_in_storage(self):
        return True

    def _data_logger_storage_items(self):
        return self.items_by_frame.get(self.frame, [])


class RetryStopHarness(OpsiVoucher):
    def __init__(self, config):
        self.config = config
        self.device = SimpleNamespace()
        self.enter_calls = 0

    def _os_voucher_enter(self):
        self.enter_calls += 1
        raise AssertionError("capped retry state must not enter the voucher shop")

    def _create_voucher_shop(self):
        raise AssertionError("capped retry state must not construct a shop")


@pytest.fixture(autouse=True)
def fixed_server_time(monkeypatch):
    monkeypatch.setattr(
        "module.config.opsi_data_logger.current_time",
        lambda _tz=None: FIXED_UTC_NOW,
    )
    monkeypatch.setattr(
        "module.config.opsi_data_logger.server_timezone",
        lambda: timedelta(hours=8),
    )
    monkeypatch.setattr(
        "module.os.tasks.voucher.get_os_next_reset",
        lambda: NEXT_RESET,
    )


def storage(config):
    return deep_get(config.data, keys=DATA_LOGGER_STORAGE_PATH, default={})


def test_item_disappearance_after_selection_without_use_is_not_success(monkeypatch):
    item = object()
    timer = FrameTimer(frame_limit=5)
    monkeypatch.setattr(
        "module.os.tasks.voucher.Timer.from_seconds",
        lambda seconds: timer
        if seconds == DATA_LOGGER_STORAGE_USE_SECONDS
        else FrameTimer(1),
    )
    task = ActivationHarness(items_by_frame={1: [item]})

    result = task._data_logger_storage_activate_item()

    assert result is DataLoggerStorageState.UNKNOWN
    assert task.clicked == [item]


def test_use_then_stable_item_disappearance_confirms_activation(monkeypatch):
    item = object()
    timer = FrameTimer(
        frame_limit=2 + DATA_LOGGER_ACTIVATION_ABSENT_FRAMES,
    )
    monkeypatch.setattr(
        "module.os.tasks.voucher.Timer.from_seconds",
        lambda seconds: timer
        if seconds == DATA_LOGGER_STORAGE_USE_SECONDS
        else FrameTimer(1),
    )
    task = ActivationHarness(
        items_by_frame={1: [item]},
        use_frames={2},
    )

    result = task._data_logger_storage_activate_item()

    assert result is DataLoggerStorageState.ACTIVATED
    assert task.clicked == [item]


def test_explicit_success_ui_confirms_activation(monkeypatch):
    item = object()
    timer = FrameTimer(frame_limit=4)
    monkeypatch.setattr(
        "module.os.tasks.voucher.Timer.from_seconds",
        lambda seconds: timer
        if seconds == DATA_LOGGER_STORAGE_USE_SECONDS
        else FrameTimer(1),
    )
    task = ActivationHarness(
        items_by_frame={1: [item]},
        use_frames={2},
        success_frames={3},
    )

    result = task._data_logger_storage_activate_item()

    assert result is DataLoggerStorageState.ACTIVATED
    assert task.clicked == [item]


def test_retry_counter_is_scoped_to_server_cycle_and_cleared_on_success():
    config = FakeConfig()

    counts = [
        data_logger_set_retry(config, f"failure_{index}")
        for index in range(1, DATA_LOGGER_MAX_FAILURES_PER_CYCLE + 1)
    ]

    assert counts == list(range(1, DATA_LOGGER_MAX_FAILURES_PER_CYCLE + 1))
    assert data_logger_retry_count(config) == DATA_LOGGER_MAX_FAILURES_PER_CYCLE
    assert storage(config)[DATA_LOGGER_RETRY_COUNT_KEY] == (
        DATA_LOGGER_MAX_FAILURES_PER_CYCLE
    )
    assert data_logger_retry_pending(config)
    assert data_logger_retry_count(
        config,
        server_now=datetime(2026, 9, 1, 0, 0),
    ) == 0

    data_logger_mark_active(config)

    assert data_logger_retry_count(config) == 0
    assert not data_logger_retry_pending(config)
    assert DATA_LOGGER_RETRY_COUNT_KEY not in storage(config)
    assert DATA_LOGGER_RETRY_PENDING_KEY not in storage(config)


def test_fifth_failure_pauses_until_month_reset_instead_of_six_hour_retry():
    config = FakeConfig()
    task = RetryStopHarness(config)

    for index in range(1, DATA_LOGGER_MAX_FAILURES_PER_CYCLE + 1):
        task._data_logger_schedule_retry(f"failure_{index}")

    assert config.delays[:-1] == [
        {
            "target": None,
            "minute": DATA_LOGGER_RETRY_MINUTES,
            "server_update": True,
        }
    ] * (DATA_LOGGER_MAX_FAILURES_PER_CYCLE - 1)
    assert config.delays[-1] == {
        "target": NEXT_RESET,
        "minute": None,
        "server_update": False,
    }
    assert data_logger_retry_count(config) == DATA_LOGGER_MAX_FAILURES_PER_CYCLE


def test_capped_retry_state_skips_shop_even_on_manual_task_run():
    config = FakeConfig()
    for index in range(DATA_LOGGER_MAX_FAILURES_PER_CYCLE):
        data_logger_set_retry(config, f"failure_{index}")
    task = RetryStopHarness(config)

    task.os_voucher()

    assert task.enter_calls == 0
    assert config.delays == [
        {
            "target": NEXT_RESET,
            "minute": None,
            "server_update": False,
        }
    ]
    assert data_logger_retry_count(config) == DATA_LOGGER_MAX_FAILURES_PER_CYCLE
