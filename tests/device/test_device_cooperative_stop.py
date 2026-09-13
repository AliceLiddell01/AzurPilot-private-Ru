from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from module.config.config import TaskEnd
from module.device.device import Device


def test_device_screenshot_boundary_observes_cooperative_stop() -> None:
    stop_event = threading.Event()
    stop_event.set()
    task_stop = Mock(side_effect=TaskEnd("Получен запрос cooperative stop"))
    device = Device.__new__(Device)
    device.config = SimpleNamespace(
        config_name="alas",
        stop_event=stop_event,
        task_stop=task_stop,
    )

    with pytest.raises(TaskEnd, match="cooperative stop"):
        device._raise_if_cooperative_stop_requested()

    task_stop.assert_called_once_with(message="Получен запрос cooperative stop")
