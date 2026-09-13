from __future__ import annotations

from types import SimpleNamespace

from module.os_handler import action_point


def test_action_point_update_keeps_total_as_python_int(monkeypatch):
    handler = object.__new__(action_point.ActionPointHandler)
    handler.device = SimpleNamespace(image=object())
    handler.config = SimpleNamespace(
        OS_ACTION_POINT_BOX_USE=True,
        update=lambda: None,
        override=lambda **_: None,
    )

    monkeypatch.setattr(
        action_point.OIL_ITEM,
        "predict",
        lambda *_args, **_kwargs: [SimpleNamespace(amount=1000)],
    )
    monkeypatch.setattr(
        action_point.ACTION_POINT_ITEMS,
        "predict",
        lambda *_args, **_kwargs: [
            SimpleNamespace(amount=1),
            SimpleNamespace(amount=2),
            SimpleNamespace(amount=3),
        ],
    )
    monkeypatch.setattr(action_point.OCR_ACTION_POINT_REMAIN, "ocr", lambda *_: 100)
    monkeypatch.setattr(action_point, "LogRes", lambda _config: SimpleNamespace())

    handler.action_point_update()

    assert handler._action_point_total == 520
    assert type(handler._action_point_total) is int
