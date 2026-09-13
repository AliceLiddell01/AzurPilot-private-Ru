from __future__ import annotations

import multiprocessing
import time
from pathlib import Path

import pytest

from module.webui.event_observation import event_observation_write_lock
from module.webui.state_lock import state_write_lock


def _hold_state_lock(lock_path: str, entered, release) -> None:
    with state_write_lock(Path(lock_path), timeout=5.0):
        entered.set()
        if not release.wait(5.0):
            raise TimeoutError("Тестовый процесс не получил сигнал освобождения блокировки")


def test_event_observation_lock_has_bounded_cross_process_timeout(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    lock_path = tmp_path / "event-observation.lock"
    entered = context.Event()
    release = context.Event()
    holder = context.Process(
        target=_hold_state_lock,
        args=(str(lock_path), entered, release),
    )
    holder.start()

    try:
        assert entered.wait(10.0)
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="Истёк тайм-аут блокировки"):
            with event_observation_write_lock(
                lock_path,
                timeout=0.2,
                retry_interval=0.02,
            ):
                pass
        assert time.monotonic() - started < 2.0
    finally:
        release.set()
        holder.join(timeout=10.0)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=5.0)

    assert holder.exitcode == 0
