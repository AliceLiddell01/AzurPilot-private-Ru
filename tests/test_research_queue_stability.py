from datetime import datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pytest

import module.research.research as research_module
import module.research.rqueue as rqueue_module
from module.combat.assets import GET_ITEMS_1, GET_ITEMS_2, GET_ITEMS_3
from module.exception import (
    GameBugError,
    GameStuckError,
    GameTooManyClickError,
    ResearchProjectStartError,
    ResearchQueueStateError,
    ResearchRewardPopupTimeoutError,
    ResearchRewardReturnTimeoutError,
)
from module.ocr.ocr import Ocr
from module.research.research import RewardResearch
from module.research.rqueue import (
    ResearchQueue,
    _parse_queue_remain_duration,
    _QUEUE_REMAIN_OCR_ATTEMPTS,
    _QUEUE_REMAIN_OCR_RECHECK_DELAY,
)
from module.research.ui import ResearchUI


def _queue_with_device():
    screenshots = []

    def screenshot():
        screenshots.append(True)

    queue = object.__new__(ResearchQueue)
    queue.device = SimpleNamespace(
        image=np.zeros((720, 1280, 3), dtype=np.uint8),
        screenshot=screenshot,
    )
    return queue, screenshots


class _ResearchDrop:
    def __init__(self):
        self.images = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def __bool__(self):
        return True

    def add(self, image):
        self.images.append(image)

    def clear(self):
        self.images.clear()


class _FastResearchTimer:
    thresholds = {
        'popup_timeout': 999,
        'popup_confirm': 3,
        'return_timeout': 999,
        'return_confirm': 2,
    }
    created = []

    def __init__(self, limit, count=0):
        self.limit = limit
        self.count = count
        self.calls = 0
        self.role = ('popup_timeout', 'popup_confirm', 'return_timeout', 'return_confirm')[len(self.created)]
        self.created.append(self)

    def start(self):
        self.calls = 0
        return self

    def reset(self):
        self.calls = 0
        return self

    def reached(self):
        self.calls += 1
        return self.calls >= self.thresholds[self.role]


def _reward_research_harness(get_items_values, statuses=None):
    research = object.__new__(RewardResearch)
    screenshots = []
    clicks = []
    known_buttons = []
    appear_calls = []
    drop = _ResearchDrop()
    values = iter(get_items_values)
    last_value = None

    def get_items():
        nonlocal last_value
        try:
            last_value = next(values)
        except StopIteration:
            pass
        return last_value

    status_values = iter(statuses or [['detail'] * len(research_module.RESEARCH_STATUS)])
    last_status = None

    def get_research_status(image):
        nonlocal last_status
        try:
            last_status = next(status_values)
        except StopIteration:
            pass
        return last_status

    research.device = SimpleNamespace(
        image=np.zeros((720, 1280, 3), dtype=np.uint8),
        screenshot=lambda: screenshots.append(True),
        click=clicks.append,
    )
    research.config = SimpleNamespace(DropRecord_ResearchRecord=True)
    research.stat = SimpleNamespace(new=lambda **kwargs: drop)
    research.get_items = get_items
    research.is_in_research = lambda: True
    research.get_research_status = get_research_status
    research.appear = lambda *args, **kwargs: appear_calls.append(args[0]) or False
    research.research_has_finished = lambda: (_ for _ in ()).throw(
        AssertionError('под модальным окном не должен обслуживаться Research underlay')
    )
    research.research_detail_quit = lambda: (_ for _ in ()).throw(
        AssertionError('под модальным окном не должен закрываться экран деталей')
    )

    def drop_record(drop, known_button=None):
        known_buttons.append(known_button)

    research.drop_record = drop_record
    return research, clicks, known_buttons, appear_calls, screenshots


def _reset_fast_research_timer(monkeypatch, **thresholds):
    _FastResearchTimer.created = []
    _FastResearchTimer.thresholds = {
        'popup_timeout': 999,
        'popup_confirm': 3,
        'return_timeout': 999,
        'return_confirm': 2,
        **thresholds,
    }
    monkeypatch.setattr(research_module, 'Timer', _FastResearchTimer)


def test_queue_duration_parser_rejects_missing_ocr_digit():
    assert _parse_queue_remain_duration('00:43:52') == timedelta(minutes=43, seconds=52)
    assert _parse_queue_remain_duration('004352') == timedelta(minutes=43, seconds=52)
    assert _parse_queue_remain_duration('00:3:52') is None
    assert _parse_queue_remain_duration('00:73:52') is None
    assert _parse_queue_remain_duration('00:43:72') is None


def test_reward_popup_masks_queue_page_detection():
    queue, _ = _queue_with_device()
    queue.appear = lambda *args, **kwargs: True

    queue.get_items = lambda: object()
    assert queue.is_in_queue() is False

    queue.get_items = lambda: None
    assert queue.is_in_queue() is True


def test_get_items_falls_back_to_two_row_popup_template():
    research = ResearchUI.__new__(ResearchUI)
    seen = []

    def appear(button, **kwargs):
        seen.append(button)
        return button == GET_ITEMS_2

    research.appear = appear
    research.image_color_count = lambda *args, **kwargs: False

    assert research.get_items() == GET_ITEMS_2
    assert seen == [GET_ITEMS_3, GET_ITEMS_2]


def test_drop_record_uses_known_popup_layout_without_redetection():
    research = ResearchUI.__new__(ResearchUI)
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    drop = _ResearchDrop()
    research.device = SimpleNamespace(image=image)
    research.get_items = lambda: (_ for _ in ()).throw(
        AssertionError('известный layout не должен повторно распознаваться')
    )

    research.drop_record(drop, known_button=GET_ITEMS_2)

    assert drop.images == [image]


def test_research_exception_hierarchy_preserves_recovery_policy():
    assert issubclass(ResearchRewardPopupTimeoutError, GameStuckError)
    assert issubclass(ResearchRewardReturnTimeoutError, GameStuckError)
    assert issubclass(ResearchProjectStartError, GameTooManyClickError)
    assert issubclass(ResearchQueueStateError, GameBugError)


def test_research_queue_state_failure_uses_domain_exception(monkeypatch):
    queue, _ = _queue_with_device()
    queue.get_items = lambda: None
    queue.image_color_count = lambda button, color, threshold, count: color == (123, 125, 123)

    with pytest.raises(ResearchQueueStateError):
        queue.get_research_ended()


def test_research_project_start_failure_uses_domain_exception(monkeypatch):
    class ClickTimer:
        def __init__(self, limit, count=0):
            self.calls = 0

        def reached(self):
            self.calls += 1
            return self.calls <= 3

        def reset(self):
            return self

    research = object.__new__(RewardResearch)
    research.device = SimpleNamespace(
        image=np.zeros((720, 1280, 3), dtype=np.uint8),
        screenshot=lambda: None,
        click=lambda button: None,
    )
    research.projects = []
    research._research_project_offset = 0
    research.interval_clear = lambda buttons: None
    research.popup_interval_clear = lambda: None
    research.image_crop = lambda *args, **kwargs: np.zeros((1, 1, 3), dtype=np.uint8)
    research.is_in_research = lambda: True
    research.appear_then_click = lambda *args, **kwargs: False
    research.handle_popup_confirm = lambda *args, **kwargs: False
    research.appear = lambda *args, **kwargs: False
    monkeypatch.setattr(research_module, 'Timer', ClickTimer)

    with pytest.raises(ResearchProjectStartError):
        research.research_project_start(0)


def test_queue_receive_finishes_detected_popup_before_queue_end(monkeypatch):
    class FakeDrop:
        def __init__(self):
            self.cleared = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return False

        def __bool__(self):
            return True

        def add(self, image):
            return None

        def clear(self):
            self.cleared = True

    class FakeTimer:
        def __init__(self, limit, count=0):
            self.limit = limit
            self.calls = 0

        def reset(self):
            self.calls = 0
            return self

        def reached(self):
            self.calls += 1
            if self.limit == 1.5:
                return self.calls >= 2
            return True

    drop = FakeDrop()
    queue = object.__new__(RewardResearch)
    clicks = []
    get_items_calls = []
    popup = object()

    queue.device = SimpleNamespace(
        image=np.zeros((720, 1280, 3), dtype=np.uint8),
        screenshot=lambda: None,
        click=clicks.append,
    )
    queue.config = SimpleNamespace(DropRecord_ResearchRecord=True)
    queue.stat = SimpleNamespace(new=lambda **kwargs: drop)
    queue.drop_record = lambda drop, known_button=None: None
    queue.is_in_queue = lambda: True
    queue.appear = lambda *args, **kwargs: False
    queue.appear_then_click = lambda *args, **kwargs: False

    def get_items():
        get_items_calls.append(True)
        return popup if len(get_items_calls) == 1 else None

    queue.get_items = get_items
    monkeypatch.setattr(research_module, 'Timer', FakeTimer)

    assert queue.queue_receive() == 1
    assert clicks == [research_module.GET_ITEMS_RESEARCH_SAVE]
    assert len(get_items_calls) == 2
    assert drop.cleared is False


def test_research_receive_tracks_layout_transition_and_saves_once(monkeypatch):
    research, clicks, known_buttons, appear_calls, _ = _reward_research_harness([
        GET_ITEMS_1,
        GET_ITEMS_2,
        GET_ITEMS_2,
        GET_ITEMS_2,
        GET_ITEMS_2,
        None,
        None,
    ])
    _reset_fast_research_timer(monkeypatch)

    assert research.research_receive() is True

    assert clicks == [research_module.GET_ITEMS_RESEARCH_SAVE]
    assert known_buttons == [GET_ITEMS_2]
    assert appear_calls == []


def test_research_receive_keeps_popup_owned_across_detector_misses(monkeypatch):
    research, clicks, known_buttons, appear_calls, _ = _reward_research_harness([
        GET_ITEMS_1,
        None,
        None,
        None,
        None,
        None,
    ])
    _reset_fast_research_timer(monkeypatch)

    assert research.research_receive() is True

    assert clicks == [research_module.GET_ITEMS_RESEARCH_SAVE]
    assert known_buttons == [GET_ITEMS_1]
    assert appear_calls == []


def test_research_receive_popup_timeout_is_research_specific(monkeypatch):
    research, clicks, _, _, _ = _reward_research_harness([
        GET_ITEMS_1,
    ])
    _reset_fast_research_timer(monkeypatch, popup_timeout=3, popup_confirm=999)

    with pytest.raises(ResearchRewardPopupTimeoutError, match='фаза=стабилизация'):
        research.research_receive()

    assert clicks == []


def test_research_receive_does_not_accept_unknown_return_state(monkeypatch):
    research, clicks, _, _, _ = _reward_research_harness(
        [
            GET_ITEMS_1,
            GET_ITEMS_1,
            GET_ITEMS_1,
            GET_ITEMS_1,
            None,
            None,
            None,
        ],
        statuses=[
            ['unknown'] * len(research_module.RESEARCH_STATUS),
            ['detail'] * len(research_module.RESEARCH_STATUS),
            ['detail'] * len(research_module.RESEARCH_STATUS),
        ],
    )
    _reset_fast_research_timer(monkeypatch)

    assert research.research_receive() is True
    assert clicks == [research_module.GET_ITEMS_RESEARCH_SAVE]


def test_research_receive_return_timeout_is_research_specific(monkeypatch):
    research, clicks, _, _, _ = _reward_research_harness([
        GET_ITEMS_1,
        GET_ITEMS_1,
        GET_ITEMS_1,
        GET_ITEMS_1,
    ], statuses=[['unknown'] * len(research_module.RESEARCH_STATUS)])
    _reset_fast_research_timer(monkeypatch, return_timeout=3, return_confirm=999)

    with pytest.raises(ResearchRewardReturnTimeoutError, match='фаза=возврат'):
        research.research_receive()

    assert clicks == [research_module.GET_ITEMS_RESEARCH_SAVE]


def test_queue_duration_retries_fresh_frames_until_valid(monkeypatch):
    queue, screenshots = _queue_with_device()
    outputs = iter(['00:3:52', '00:3:51', '00:43:50'])

    monkeypatch.setattr(
        Ocr,
        'ocr',
        lambda self, image, direct_ocr=False: next(outputs),
    )

    assert queue._read_queue_remain_duration() == timedelta(minutes=43, seconds=50)
    assert len(screenshots) == 2


def test_queue_duration_stops_after_bounded_invalid_attempts(monkeypatch):
    queue, screenshots = _queue_with_device()

    monkeypatch.setattr(
        Ocr,
        'ocr',
        lambda self, image, direct_ocr=False: '00:3:52',
    )

    assert queue._read_queue_remain_duration() is None
    assert len(screenshots) == _QUEUE_REMAIN_OCR_ATTEMPTS - 1


def test_get_research_ended_uses_valid_duration(monkeypatch):
    queue, _ = _queue_with_device()
    now = datetime(2026, 8, 14, 22, 21, 0)

    queue.get_items = lambda: None

    def image_color_count(button, color, threshold, count):
        if color == (123, 125, 123):
            return False
        if color == (255, 255, 255):
            return True
        raise AssertionError(f'Неожиданный цвет: {color}')

    queue.image_color_count = image_color_count
    queue._read_queue_remain_duration = lambda: timedelta(minutes=43, seconds=44)
    monkeypatch.setattr(rqueue_module, 'current_time', lambda: now)

    assert queue.get_research_ended() == now + timedelta(minutes=43, seconds=44)


def test_get_research_ended_defers_when_reward_popup_is_open(monkeypatch):
    queue, _ = _queue_with_device()
    now = datetime(2026, 8, 14, 22, 21, 0)

    queue.get_items = lambda: object()
    queue.image_color_count = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError('Цветовая диагностика не должна выполняться под окном награды')
    )
    monkeypatch.setattr(rqueue_module, 'current_time', lambda: now)

    assert queue.get_research_ended() == now + _QUEUE_REMAIN_OCR_RECHECK_DELAY


def test_invalid_queue_duration_uses_non_immediate_recheck(monkeypatch):
    queue, _ = _queue_with_device()
    now = datetime(2026, 8, 14, 22, 21, 0)

    queue.get_items = lambda: None

    def image_color_count(button, color, threshold, count):
        if color == (123, 125, 123):
            return False
        if color == (255, 255, 255):
            return True
        raise AssertionError(f'Неожиданный цвет: {color}')

    queue.image_color_count = image_color_count
    queue._read_queue_remain_duration = lambda: None
    monkeypatch.setattr(rqueue_module, 'current_time', lambda: now)

    assert _QUEUE_REMAIN_OCR_RECHECK_DELAY > timedelta(minutes=10)
    assert queue.get_research_ended() == now + _QUEUE_REMAIN_OCR_RECHECK_DELAY
