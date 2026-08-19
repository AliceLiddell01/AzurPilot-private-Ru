from module.retire.assets import ONE_CLICK_RETIREMENT
from module.retire.retirement import Retirement


def _retirement_without_runtime():
    return Retirement.__new__(Retirement)


def test_one_click_retirement_uses_color_detection_first(monkeypatch):
    retirement = _retirement_without_runtime()
    appear_calls = []
    clear_calls = []

    def appear_then_click(button, **kwargs):
        appear_calls.append((button, kwargs))
        return True

    monkeypatch.setattr(retirement, 'appear_then_click', appear_then_click)
    monkeypatch.setattr(ONE_CLICK_RETIREMENT, 'clear_offset', lambda: clear_calls.append(True))

    assert retirement._one_click_retirement_click() is True
    assert appear_calls == [(ONE_CLICK_RETIREMENT, {'interval': 2})]
    assert len(clear_calls) == 1


def test_one_click_retirement_falls_back_to_shifted_template(monkeypatch):
    retirement = _retirement_without_runtime()
    appear_calls = []
    clear_calls = []
    results = iter((False, True))

    def appear_then_click(button, **kwargs):
        appear_calls.append((button, kwargs))
        return next(results)

    monkeypatch.setattr(retirement, 'appear_then_click', appear_then_click)
    monkeypatch.setattr(ONE_CLICK_RETIREMENT, 'clear_offset', lambda: clear_calls.append(True))

    assert retirement._one_click_retirement_click() is True
    assert appear_calls == [
        (ONE_CLICK_RETIREMENT, {'interval': 2}),
        (ONE_CLICK_RETIREMENT, {'offset': (20, 20), 'interval': 2}),
    ]
    assert len(clear_calls) == 2


def test_one_click_retirement_clears_offset_after_failed_detection(monkeypatch):
    retirement = _retirement_without_runtime()
    clear_calls = []

    monkeypatch.setattr(retirement, 'appear_then_click', lambda _button, **_kwargs: False)
    monkeypatch.setattr(ONE_CLICK_RETIREMENT, 'clear_offset', lambda: clear_calls.append(True))

    assert retirement._one_click_retirement_click() is False
    assert len(clear_calls) == 3
