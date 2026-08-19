import module.retire.retirement as retirement_module
from module.combat.assets import GET_ITEMS_1
from module.retire.assets import (
    EQUIP_CONFIRM,
    EQUIP_CONFIRM_2,
    GET_ITEMS_1_RETIREMENT_SAVE,
    IN_RETIREMENT_CHECK,
    SHIP_CONFIRM,
    SHIP_CONFIRM_2,
)
from module.retire.retirement import Retirement


def _retirement_without_runtime():
    return Retirement.__new__(Retirement)


class _FakeTimer:
    instances = []

    def __init__(self, limit, count=0):
        self.limit = limit
        self.count = count
        self._started = False
        self._access = 0
        self.timed_out = False
        self.__class__.instances.append(self)

    @classmethod
    def from_seconds(cls, limit, speed=0.5):
        return cls(limit, count=int(limit / speed))

    def start(self):
        if not self._started:
            self._started = True
            self._access = 0
        return self

    def reached(self):
        self._access += 1
        if self.limit >= 10:
            if self._access > 3:
                self.timed_out = True
                return True
            return False
        return self._access > 1

    def reset(self):
        self._started = True
        self._access = 0
        return self

    def clear(self):
        self._started = False
        self._access = 0
        return self


class _Config:
    SERVER = 'en'
    OldRetire_SR = False
    OldRetire_SSR = False
    Retirement_RetireMode = 'one_click_retire'

    @staticmethod
    def is_task_enabled(_task):
        return False


def _prepare_confirmation_runtime(monkeypatch, retirement, phase, reward_phases):
    retirement.config = _Config()
    retirement._unable_to_enhance = False
    retirement._have_kept_cv = True

    class Device:
        image = None

        @staticmethod
        def screenshot():
            return None

        @staticmethod
        def click(button):
            if button is SHIP_CONFIRM_2:
                phase['value'] = 'reward_retire'
            elif button is GET_ITEMS_1_RETIREMENT_SAVE:
                if phase['value'] == 'reward_retire':
                    phase['value'] = 'equip' if 'reward_equip' in reward_phases else 'done'
                elif phase['value'] == 'reward_equip':
                    phase['value'] = 'done'

    retirement.device = Device()

    monkeypatch.setattr(retirement_module, 'Timer', _FakeTimer)
    monkeypatch.setattr(retirement, 'interval_clear', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(retirement, 'interval_reset', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(retirement, 'popup_interval_clear', lambda: None)
    monkeypatch.setattr(retirement, 'handle_popup_confirm', lambda **_kwargs: False)
    monkeypatch.setattr(
        retirement,
        '_retirement_get_items_appear',
        lambda: phase['value'] in reward_phases,
    )

    def appear(button, **_kwargs):
        if button is IN_RETIREMENT_CHECK:
            return phase['value'] in {'equip', 'done'}
        if button is EQUIP_CONFIRM:
            return phase['value'] == 'equip'
        if button is EQUIP_CONFIRM_2:
            return False
        return False

    def match_template_color(button, **_kwargs):
        return button is SHIP_CONFIRM_2 and phase['value'] == 'ship'

    def appear_then_click(button, **_kwargs):
        if button is EQUIP_CONFIRM and phase['value'] == 'equip':
            phase['value'] = 'reward_equip'
            return True
        return False

    monkeypatch.setattr(retirement, 'appear', appear)
    monkeypatch.setattr(retirement, 'match_template_color', match_template_color)
    monkeypatch.setattr(retirement, 'appear_then_click', appear_then_click)


def test_retirement_get_items_uses_color_detection_first(monkeypatch):
    retirement = _retirement_without_runtime()
    appear_calls = []
    clear_calls = []

    def appear(button, **kwargs):
        appear_calls.append((button, kwargs))
        return True

    monkeypatch.setattr(retirement, 'appear', appear)
    monkeypatch.setattr(GET_ITEMS_1, 'clear_offset', lambda: clear_calls.append(True))

    assert retirement._retirement_get_items_appear() is True
    assert appear_calls == [(GET_ITEMS_1, {'interval': 2, 'threshold': 20})]
    assert len(clear_calls) == 1


def test_retirement_get_items_falls_back_to_template(monkeypatch):
    retirement = _retirement_without_runtime()
    appear_calls = []
    clear_calls = []
    results = iter((False, True))

    def appear(button, **kwargs):
        appear_calls.append((button, kwargs))
        return next(results)

    monkeypatch.setattr(retirement, 'appear', appear)
    monkeypatch.setattr(GET_ITEMS_1, 'clear_offset', lambda: clear_calls.append(True))

    assert retirement._retirement_get_items_appear() is True
    assert appear_calls == [
        (GET_ITEMS_1, {'interval': 2, 'threshold': 20}),
        (GET_ITEMS_1, {'offset': (30, 30), 'interval': 2}),
    ]
    assert len(clear_calls) == 2


def test_retirement_get_items_clears_offset_after_failed_detection(monkeypatch):
    retirement = _retirement_without_runtime()
    appear_calls = []
    clear_calls = []

    def appear(button, **kwargs):
        appear_calls.append((button, kwargs))
        return False

    monkeypatch.setattr(retirement, 'appear', appear)
    monkeypatch.setattr(GET_ITEMS_1, 'clear_offset', lambda: clear_calls.append(True))

    assert retirement._retirement_get_items_appear() is False
    assert appear_calls == [
        (GET_ITEMS_1, {'interval': 2, 'threshold': 20}),
        (GET_ITEMS_1, {'offset': (30, 30), 'interval': 2}),
    ]
    assert len(clear_calls) == 3


def test_retirement_confirm_finishes_no_equipment_without_global_timeout(monkeypatch):
    _FakeTimer.instances = []
    retirement = _retirement_without_runtime()
    phase = {'value': 'ship'}
    _prepare_confirmation_runtime(
        monkeypatch,
        retirement,
        phase,
        reward_phases={'reward_retire'},
    )

    retirement._retirement_confirm()

    assert phase['value'] == 'done'
    global_timeout = next(timer for timer in _FakeTimer.instances if timer.limit >= 10)
    assert global_timeout.timed_out is False


def test_retirement_confirm_finishes_after_single_equipment_confirm(monkeypatch):
    _FakeTimer.instances = []
    retirement = _retirement_without_runtime()
    phase = {'value': 'ship'}
    _prepare_confirmation_runtime(
        monkeypatch,
        retirement,
        phase,
        reward_phases={'reward_retire', 'reward_equip'},
    )

    retirement._retirement_confirm()

    assert phase['value'] == 'done'
    global_timeout = next(timer for timer in _FakeTimer.instances if timer.limit >= 10)
    assert global_timeout.timed_out is False
