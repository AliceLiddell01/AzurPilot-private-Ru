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
from module.research.assets import RESEARCH_START, RESEARCH_STOP
from module.research.selector import RESEARCH_ENTRANCE
from module.research.ui import ResearchUI
from module.ui.assets import RESEARCH_CHECK


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
        'timeout': 999,
        'popup_confirm': 3,
        'return_confirm': 2,
    }
    created = []

    def __init__(self, limit, count=0):
        self.limit = limit
        self.count = count
        self.calls = 0
        parameters = (limit, count)
        popup_timeout = (
            research_module._RESEARCH_REWARD_POPUP_TIMEOUT_SECONDS,
            research_module._RESEARCH_REWARD_POPUP_TIMEOUT_COUNT,
        )
        return_timeout = (
            research_module._RESEARCH_REWARD_RETURN_TIMEOUT_SECONDS,
            research_module._RESEARCH_REWARD_RETURN_TIMEOUT_COUNT,
        )
        popup_confirm = (
            research_module._RESEARCH_REWARD_POPUP_STABILIZATION_SECONDS,
            research_module._RESEARCH_REWARD_POPUP_STABILIZATION_COUNT,
        )
        return_confirm = (
            research_module._RESEARCH_REWARD_RETURN_CONFIRM_SECONDS,
            research_module._RESEARCH_REWARD_RETURN_CONFIRM_COUNT,
        )
        if parameters == popup_timeout or parameters == return_timeout:
            self.role = 'timeout'
            max_instances = int(parameters == popup_timeout) + int(parameters == return_timeout)
        elif parameters == popup_confirm:
            self.role = 'popup_confirm'
            max_instances = 1
        elif parameters == return_confirm:
            self.role = 'return_confirm'
            max_instances = 1
        else:
            raise AssertionError(f'Неожиданные параметры Timer: limit={limit}, count={count}')
        created_with_parameters = sum(
            (timer.limit, timer.count) == parameters for timer in self.created
        )
        if created_with_parameters >= max_instances:
            raise AssertionError(f'Слишком много Timer с параметрами: limit={limit}, count={count}')
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
        'timeout': 999,
        'popup_confirm': 3,
        'return_confirm': 2,
        **thresholds,
    }
    monkeypatch.setattr(research_module, 'Timer', _FastResearchTimer)


class _IncidentResearchHarness:
    """Явная машина состояний для перехода из Research в окно награды."""

    def __init__(self, finished_index=1):
        self.finished_index = finished_index
        self.expected_entrance = RESEARCH_ENTRANCE[finished_index]
        self.phase = 'underlay'
        self.screenshots = []
        self.post_save_screenshots = []
        self.clicks = []
        self.entrance_clicks = []
        self.save_clicks = []
        self.get_items_calls = []
        self.appear_calls = []
        self.popup_appear_calls = []
        self.return_appear_calls = []
        self.research_has_finished_calls = 0
        self.research_detail_quit_calls = 0
        self.drop_layouts = []
        self.status_calls = []
        self.drop = _ResearchDrop()
        self._popup_values = iter([GET_ITEMS_1, None, None, None])
        self._return_values = iter([None, None, None])
        self._return_statuses = iter([
            ['unknown'] * len(research_module.RESEARCH_STATUS),
            ['detail'] * len(research_module.RESEARCH_STATUS),
            ['detail'] * len(research_module.RESEARCH_STATUS),
        ])

        self.research = object.__new__(RewardResearch)
        self.research._research_finished_index = finished_index
        self.research.device = SimpleNamespace(
            image=np.zeros((720, 1280, 3), dtype=np.uint8),
            screenshot=self.screenshot,
            click=self.click,
        )
        self.research.config = SimpleNamespace(DropRecord_ResearchRecord=True)
        self.research.stat = SimpleNamespace(new=lambda **kwargs: self.drop)
        self.research.get_items = self.get_items
        self.research.is_in_research = self.is_in_research
        self.research.get_research_status = self.get_research_status
        self.research.appear = self.appear
        self.research.research_has_finished = self.research_has_finished
        self.research.research_detail_quit = self.research_detail_quit
        self.research.drop_record = self.drop_record

    def screenshot(self):
        if self.phase == 'entrance_pending':
            self.phase = 'popup'
        self.screenshots.append(self.phase)
        if self.phase == 'return':
            self.post_save_screenshots.append(self.phase)

    def click(self, button):
        self.clicks.append(button)
        if button is self.expected_entrance:
            self.entrance_clicks.append(button)
            self.phase = 'entrance_pending'
        elif button is research_module.GET_ITEMS_RESEARCH_SAVE:
            self.save_clicks.append(button)
            self.phase = 'return'
        else:
            raise AssertionError(f'Неожиданный клик в тестовом стенде: {button}')

    def get_items(self):
        if self.phase in ('underlay', 'entrance_pending'):
            value = None
        elif self.phase == 'popup':
            try:
                value = next(self._popup_values)
            except StopIteration:
                value = None
        elif self.phase == 'return':
            try:
                value = next(self._return_values)
            except StopIteration:
                value = None
        else:
            raise AssertionError(f'Неизвестная фаза тестового стенда: {self.phase}')
        self.get_items_calls.append((self.phase, value))
        return value

    def appear(self, button, **kwargs):
        self.appear_calls.append((button, self.phase))
        if self.phase == 'popup':
            self.popup_appear_calls.append(button)
        elif self.phase == 'return':
            self.return_appear_calls.append(button)
        return self.phase == 'underlay' and button is RESEARCH_CHECK

    def is_in_research(self):
        return self.phase == 'return'

    def get_research_status(self, image):
        try:
            status = next(self._return_statuses)
        except StopIteration:
            status = ['unknown'] * len(research_module.RESEARCH_STATUS)
        self.status_calls.append(status)
        return status

    def research_has_finished(self):
        assert self.phase == 'underlay'
        self.research_has_finished_calls += 1
        self.research._research_finished_index = self.finished_index
        return True

    def research_detail_quit(self):
        self.research_detail_quit_calls += 1
        raise AssertionError('Под окном награды нельзя закрывать экран деталей')

    def drop_record(self, drop, known_button=None):
        self.drop_layouts.append(known_button)
        assert known_button is GET_ITEMS_1


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
    research.ensure_research_stable = lambda: None
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


def test_research_receive_reproduces_incident_transition(monkeypatch):
    harness = _IncidentResearchHarness(finished_index=1)
    _reset_fast_research_timer(monkeypatch)

    assert harness.research.research_receive() is True

    assert harness.entrance_clicks == [harness.expected_entrance]
    assert harness.clicks == [harness.expected_entrance, research_module.GET_ITEMS_RESEARCH_SAVE]
    assert harness.save_clicks == [research_module.GET_ITEMS_RESEARCH_SAVE]
    assert harness.screenshots[:4] == ['popup'] * 4
    assert harness.get_items_calls == [
        ('underlay', None),
        ('popup', GET_ITEMS_1),
        ('popup', None),
        ('popup', None),
        ('popup', None),
        ('return', None),
        ('return', None),
        ('return', None),
    ]
    assert harness.appear_calls == [(RESEARCH_CHECK, 'underlay')]
    assert not any(
        button is underlay_button
        for button in harness.popup_appear_calls
        for underlay_button in (RESEARCH_CHECK, RESEARCH_START, RESEARCH_STOP)
    )
    assert harness.return_appear_calls == []
    assert harness.research_has_finished_calls == 1
    assert harness.research_detail_quit_calls == 0
    assert harness.drop_layouts == [GET_ITEMS_1]
    assert harness.post_save_screenshots == ['return', 'return', 'return']
    assert harness.status_calls == [
        ['unknown'] * len(research_module.RESEARCH_STATUS),
        ['detail'] * len(research_module.RESEARCH_STATUS),
        ['detail'] * len(research_module.RESEARCH_STATUS),
    ]


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
    _reset_fast_research_timer(monkeypatch, timeout=3, popup_confirm=999)

    with pytest.raises(ResearchRewardPopupTimeoutError, match='фаза=стабилизация'):
        research.research_receive()

    assert clicks == []


def test_research_receive_requires_stable_known_return_frames(monkeypatch):
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
            ['detail'] * len(research_module.RESEARCH_STATUS),
            ['unknown'] * len(research_module.RESEARCH_STATUS),
            ['detail'] * len(research_module.RESEARCH_STATUS),
            ['detail'] * len(research_module.RESEARCH_STATUS),
        ],
    )
    status_calls = []
    get_research_status = research.get_research_status
    research.get_research_status = lambda image: (
        status_calls.append(True) or get_research_status(image)
    )
    _reset_fast_research_timer(monkeypatch)

    assert research.research_receive() is True
    assert clicks == [research_module.GET_ITEMS_RESEARCH_SAVE]
    assert len(status_calls) == 4


def test_research_receive_resets_return_confirmation_when_popup_reappears(monkeypatch):
    research, clicks, _, _, _ = _reward_research_harness(
        [
            GET_ITEMS_1,
            GET_ITEMS_1,
            GET_ITEMS_1,
            GET_ITEMS_1,
            None,
            GET_ITEMS_1,
            None,
            None,
        ],
        statuses=[['detail'] * len(research_module.RESEARCH_STATUS)] * 3,
    )
    get_items_values = []
    get_items = research.get_items

    def tracked_get_items():
        value = get_items()
        get_items_values.append(value)
        return value

    research.get_items = tracked_get_items
    status_calls = []
    get_research_status = research.get_research_status
    research.get_research_status = lambda image: (
        status_calls.append(True) or get_research_status(image)
    )
    _reset_fast_research_timer(monkeypatch)

    assert research.research_receive() is True

    assert clicks == [research_module.GET_ITEMS_RESEARCH_SAVE]
    assert get_items_values[-4:] == [None, GET_ITEMS_1, None, None]
    assert len(status_calls) == 3


def test_research_receive_return_timeout_is_research_specific(monkeypatch):
    research, clicks, _, _, _ = _reward_research_harness([
        GET_ITEMS_1,
        GET_ITEMS_1,
        GET_ITEMS_1,
        GET_ITEMS_1,
    ], statuses=[['unknown'] * len(research_module.RESEARCH_STATUS)])
    _reset_fast_research_timer(monkeypatch, timeout=4, return_confirm=999)

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
