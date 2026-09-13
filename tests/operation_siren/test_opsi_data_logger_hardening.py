from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from module.combat.assets import GET_ITEMS_1, GET_ITEMS_2
from module.config.deep import deep_get, deep_set
from module.config.opsi_data_logger import (
    DATA_LOGGER_EVIDENCE_CYCLE_KEY,
    DATA_LOGGER_EVIDENCE_KEY,
    DATA_LOGGER_STORAGE_PATH,
    DataLoggerLifecycleEvidence,
    DataLoggerPurchaseEvidence,
    DataLoggerShopResult,
    DataLoggerShopState,
    DataLoggerStorageState,
    data_logger_lifecycle_evidence,
    data_logger_mark_active,
    data_logger_mark_evidence,
)
from module.handler.assets import GET_MISSION
from module.os.tasks.voucher import (
    DATA_LOGGER_STORAGE_ENTER_SECONDS,
    DATA_LOGGER_STORAGE_NO_SCROLLBAR_FRAMES,
    DATA_LOGGER_STORAGE_USE_SECONDS,
    OpsiVoucher,
)
from module.os_handler.assets import (
    AUTO_SEARCH_REWARD,
    GET_ADAPTABILITY,
    STORAGE_ENTER,
    STORAGE_USE,
)
from module.os_handler.storage import SCROLL_STORAGE
from module.storage.assets import BOX_USE

FIXED_UTC_NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)


class FakeConfig:
    def __init__(self):
        self.data = {
            'OpsiExplore': {
                'OpsiExplore': {'SpecialRadar': True},
                'Storage': {'Storage': {}},
            }
        }

    def cross_get(self, keys, default=None):
        return deep_get(self.data, keys=keys, default=default)

    def cross_set(self, keys, value):
        deep_set(self.data, keys=keys, value=value)


class FrameTimer:
    def __init__(self, frame_limit: int):
        self.frame_limit = frame_limit
        self.calls = 0

    def start(self):
        return self

    def reached(self):
        self.calls += 1
        return self.calls > self.frame_limit


class RewardBeforeUseHarness(OpsiVoucher):
    def __init__(self):
        self.frame = 0
        self.item = object()
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
        if button is GET_MISSION or button is GET_ADAPTABILITY:
            return False
        return False

    def appear_then_click(self, button, **_kwargs):
        if button is GET_ITEMS_1:
            return self.frame == 2
        if button in {GET_ITEMS_2, STORAGE_USE, BOX_USE}:
            return False
        return False

    def handle_story_skip(self):
        return False

    def is_in_storage(self):
        return True

    def _data_logger_storage_items(self):
        return [self.item] if self.frame == 1 else []


class ScrollHarness(OpsiVoucher):
    def __init__(self, storage_frames: int):
        self.frame = 0
        self.storage_frames = storage_frames
        self.sleeps = []
        self.device = SimpleNamespace(
            screenshot=self._screenshot,
            sleep=self.sleeps.append,
            swipe=lambda *_args, **_kwargs: None,
        )

    def _screenshot(self):
        self.frame += 1

    def is_in_storage(self):
        return self.frame <= self.storage_frames


class LostMapHarness(OpsiVoucher):
    def __init__(self):
        self.shots = 0
        self.zone = SimpleNamespace(is_azur_port=True)
        self.device = SimpleNamespace(
            screenshot=self._screenshot,
            click=lambda _button: None,
        )

    def _screenshot(self):
        self.shots += 1

    def _data_logger_ensure_port_map(self):
        return True

    def interval_clear(self, _button):
        return None

    def is_in_storage(self):
        return False

    def appear(self, *_args, **_kwargs):
        return False

    def appear_then_click(self, *_args, **_kwargs):
        return False

    def handle_map_event(self):
        return False

    def is_in_map(self):
        return False


class StorageTransitionHarness(OpsiVoucher):
    def __init__(self):
        self.frame = 0
        self.storage_clicks = 0
        self.zone = SimpleNamespace(is_azur_port=True)
        self.device = SimpleNamespace(
            screenshot=self._screenshot,
            click=lambda _button: None,
        )

    def _screenshot(self):
        self.frame += 1

    def _data_logger_ensure_port_map(self):
        return True

    def interval_clear(self, _button):
        return None

    def is_in_storage(self):
        return self.frame >= 4

    def handle_info_bar(self):
        return None

    def appear(self, *_args, **_kwargs):
        return False

    def appear_then_click(self, button, **_kwargs):
        if button is AUTO_SEARCH_REWARD:
            return False
        if button is STORAGE_ENTER and self.frame == 1:
            self.storage_clicks += 1
            return True
        return False

    def handle_map_event(self):
        return False

    def is_in_map(self):
        return self.frame in {1, 2}


@pytest.fixture(autouse=True)
def fixed_server_time(monkeypatch):
    monkeypatch.setattr(
        'module.config.opsi_data_logger.current_time',
        lambda _tz=None: FIXED_UTC_NOW,
    )
    monkeypatch.setattr(
        'module.config.opsi_data_logger.server_timezone',
        lambda: timedelta(hours=8),
    )


def storage(config):
    return deep_get(config.data, keys=DATA_LOGGER_STORAGE_PATH, default={})


def test_legacy_purchase_flag_is_mapped_to_graded_evidence():
    timeout = DataLoggerShopResult(
        DataLoggerShopState.UNKNOWN,
        reason='purchase_timeout',
        purchased=True,
    )
    confirmed = DataLoggerShopResult(
        DataLoggerShopState.SOLD_OUT,
        reason='dimmed_target',
        purchased=True,
    )
    untouched = DataLoggerShopResult(DataLoggerShopState.SOLD_OUT)

    assert timeout.purchase_evidence is DataLoggerPurchaseEvidence.ATTEMPTED
    assert confirmed.purchase_evidence is DataLoggerPurchaseEvidence.CONFIRMED
    assert untouched.purchase_evidence is DataLoggerPurchaseEvidence.NONE


def test_exact_item_evidence_is_monotonic_and_cleared_on_success():
    config = FakeConfig()

    data_logger_mark_evidence(
        config,
        DataLoggerLifecycleEvidence.STORAGE_OBSERVED,
    )
    data_logger_mark_evidence(
        config,
        DataLoggerLifecycleEvidence.USE_CLICKED,
    )
    data_logger_mark_evidence(
        config,
        DataLoggerLifecycleEvidence.STORAGE_OBSERVED,
    )

    assert data_logger_lifecycle_evidence(config) is (
        DataLoggerLifecycleEvidence.USE_CLICKED
    )
    assert storage(config)[DATA_LOGGER_EVIDENCE_KEY] == 'use_clicked'
    assert storage(config)[DATA_LOGGER_EVIDENCE_CYCLE_KEY] == '2026-08'

    data_logger_mark_active(config)

    assert data_logger_lifecycle_evidence(config) is DataLoggerLifecycleEvidence.NONE
    assert DATA_LOGGER_EVIDENCE_KEY not in storage(config)
    assert DATA_LOGGER_EVIDENCE_CYCLE_KEY not in storage(config)


def test_generic_reward_popup_before_use_is_not_activation(monkeypatch):
    timer = FrameTimer(frame_limit=5)
    monkeypatch.setattr(
        'module.os.tasks.voucher.Timer.from_seconds',
        lambda seconds: timer
        if seconds == DATA_LOGGER_STORAGE_USE_SECONDS
        else FrameTimer(1),
    )
    task = RewardBeforeUseHarness()

    result = task._data_logger_storage_activate_item()

    assert result is DataLoggerStorageState.UNKNOWN
    assert task.clicked == [task.item]


def test_missing_scrollbar_requires_stable_storage_frames(monkeypatch):
    timer = FrameTimer(frame_limit=6)
    monkeypatch.setattr(
        'module.os.tasks.voucher.Timer.from_seconds',
        lambda _seconds: timer,
    )
    monkeypatch.setattr(SCROLL_STORAGE, 'appear', lambda main: False)
    task = ScrollHarness(storage_frames=2)

    assert not task._data_logger_storage_scroll_bottom()
    assert task.frame == 3
    assert len(task.sleeps) == DATA_LOGGER_STORAGE_NO_SCROLLBAR_FRAMES - 1


def test_stable_short_storage_without_scrollbar_is_accepted(monkeypatch):
    timer = FrameTimer(frame_limit=6)
    monkeypatch.setattr(
        'module.os.tasks.voucher.Timer.from_seconds',
        lambda _seconds: timer,
    )
    monkeypatch.setattr(SCROLL_STORAGE, 'appear', lambda main: False)
    task = ScrollHarness(storage_frames=10)

    assert task._data_logger_storage_scroll_bottom()
    assert task.frame == DATA_LOGGER_STORAGE_NO_SCROLLBAR_FRAMES


def test_storage_entry_aborts_when_allied_port_map_invariant_is_lost(monkeypatch):
    timer = FrameTimer(frame_limit=5)
    monkeypatch.setattr(
        'module.os.tasks.voucher.Timer.from_seconds',
        lambda seconds: timer
        if seconds == DATA_LOGGER_STORAGE_ENTER_SECONDS
        else FrameTimer(1),
    )
    task = LostMapHarness()

    assert not task._data_logger_storage_enter()
    assert task.shots == 1


def test_storage_entry_keeps_transition_grace_until_storage_appears(monkeypatch):
    overall = FrameTimer(frame_limit=8)
    transition = FrameTimer(frame_limit=4)

    def timer_factory(seconds):
        if seconds == DATA_LOGGER_STORAGE_ENTER_SECONDS:
            return overall
        return transition

    monkeypatch.setattr(
        'module.os.tasks.voucher.Timer.from_seconds',
        timer_factory,
    )
    task = StorageTransitionHarness()

    assert task._data_logger_storage_enter()
    assert task.storage_clicks == 1
    assert task.frame == 4
