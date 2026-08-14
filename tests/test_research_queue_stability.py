from datetime import datetime, timedelta
from types import SimpleNamespace

import numpy as np

import module.research.research as research_module
import module.research.rqueue as rqueue_module
from module.ocr.ocr import Ocr
from module.research.research import RewardResearch
from module.research.rqueue import (
    ResearchQueue,
    _parse_queue_remain_duration,
    _QUEUE_REMAIN_OCR_ATTEMPTS,
    _QUEUE_REMAIN_OCR_RECHECK_DELAY,
)


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
    queue.drop_record = lambda drop: None
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
