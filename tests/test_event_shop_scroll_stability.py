from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from module.exception import GameStuckError
from module.shop_event.ui import EventShopScroll
from module.ui.scroll import Scroll


def make_scroll():
    return EventShopScroll(
        (0, 0, 10, 100),
        color=(44, 48, 56),
        name="TEST_EVENT_SHOP_SCROLL",
    )


def make_structured_frame(seed=42):
    rng = np.random.default_rng(seed)
    image = rng.integers(0, 256, (160, 240, 3), dtype=np.uint8)
    return cv2.GaussianBlur(image, (5, 5), 0)


def test_content_stability_rejects_global_motion_but_tolerates_local_animation():
    previous = make_structured_frame()

    moving = np.roll(previous, 6, axis=0)
    assert EventShopScroll._content_frames_stable(previous, moving) is False

    local_animation = previous.copy()
    rng = np.random.default_rng(7)
    local_animation[20:60, 20:70] = rng.integers(
        0,
        256,
        (40, 50, 3),
        dtype=np.uint8,
    )
    assert EventShopScroll._content_frames_stable(previous, local_animation) is True

    assert EventShopScroll._content_frames_stable(previous, previous.copy()) is True


def test_content_shift_reports_vertical_geometry_motion():
    previous = make_structured_frame()
    moving = np.roll(previous, 5, axis=0)

    shift = EventShopScroll._content_shift(previous, moving)

    assert shift is not None
    dx, dy, response = shift
    assert abs(dx) < 1.0
    assert dy == pytest.approx(5.0, abs=0.5)
    assert response > 0.1


def test_regular_scroll_waits_for_content_only_after_real_drag(monkeypatch):
    scroll = make_scroll()
    main = SimpleNamespace()
    waits = []
    drag_results = iter((1, 0))

    def fake_set(self, position, main, **kwargs):
        return next(drag_results)

    monkeypatch.setattr(Scroll, "set", fake_set)
    monkeypatch.setattr(
        scroll,
        "wait_content_stable",
        lambda target: waits.append(target) or True,
    )

    assert scroll.set(0.5, main=main) == 1
    assert waits == [main]

    assert scroll.set(0.5, main=main) == 0
    assert waits == [main]


def test_regular_scroll_fails_closed_when_content_does_not_settle(monkeypatch):
    scroll = make_scroll()
    main = SimpleNamespace()

    monkeypatch.setattr(Scroll, "set", lambda self, position, main, **kwargs: 1)
    monkeypatch.setattr(scroll, "wait_content_stable", lambda target: False)

    with pytest.raises(GameStuckError, match="OCR заблокирован"):
        scroll.set(0.5, main=main)


def test_precise_scroll_requires_visual_settle_even_without_new_drag(monkeypatch):
    scroll = make_scroll()
    main = SimpleNamespace()
    calls = []

    def fake_set(self, position, main, **kwargs):
        calls.append((position, kwargs.get("random_range"), self.drag_threshold))
        return 0

    monkeypatch.setattr(Scroll, "set", fake_set)
    monkeypatch.setattr(scroll, "wait_content_stable", lambda target: False)

    with pytest.raises(GameStuckError, match="Карточки не стабилизировались"):
        scroll.set_precise(0.3007513823848454, main=main)

    assert calls == [(0.3007513823848454, (0.0, 0.0), 0.02)]


def test_precise_scroll_returns_only_after_successful_visual_settle(monkeypatch):
    scroll = make_scroll()
    main = SimpleNamespace()
    waits = []

    monkeypatch.setattr(Scroll, "set", lambda self, position, main, **kwargs: 1)
    monkeypatch.setattr(
        scroll,
        "wait_content_stable",
        lambda target: waits.append(target) or True,
    )

    assert scroll.set_precise(0.7, main=main) == 1
    assert waits == [main]
