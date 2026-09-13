"""Глобальные pytest-настройки тестовой платформы.

Подсистемные fixtures находятся рядом с владельцами. В корневом conftest.py
нет общего состояния тестов или собственного планировщика параллельного
запуска; здесь остаётся только изоляция process-global подмены PIL при сборе.
"""

from __future__ import annotations

import sys

import pytest

_MISSING = object()
_PIL_BEFORE: dict[str, tuple[object, object]] = {}


def _is_webui_fake_pil(module: object) -> bool:
    if module is _MISSING or module is None:
        return False
    if getattr(module, "__file__", None) is not None:
        return False
    image_module = getattr(module, "Image", None)
    image_type = getattr(image_module, "Image", None)
    return getattr(image_type, "__name__", "") == "MockPILImage"


def _restore_module(name: str, previous: object) -> None:
    if previous is _MISSING:
        sys.modules.pop(name, None)
    else:
        sys.modules[name] = previous


def pytest_collectstart(collector: pytest.Collector) -> None:
    if isinstance(collector, pytest.Module):
        _PIL_BEFORE[collector.nodeid] = (
            sys.modules.get("PIL", _MISSING),
            sys.modules.get("PIL.Image", _MISSING),
        )


def pytest_collectreport(report: pytest.CollectReport) -> None:
    previous = _PIL_BEFORE.pop(report.nodeid, None)
    if previous is None:
        return

    # WebUI намеренно подменяет PIL перед импортом PyWebIO. В production это
    # process-global оптимизация, но между test-модулями состояние протекать не должно.
    current_pil = sys.modules.get("PIL", _MISSING)
    if not _is_webui_fake_pil(current_pil):
        return

    previous_pil, previous_image = previous
    _restore_module("PIL", previous_pil)
    _restore_module("PIL.Image", previous_image)
