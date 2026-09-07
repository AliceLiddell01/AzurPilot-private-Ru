from __future__ import annotations

import pytest

from module.application.game_models import CurrentTaskSnapshot, CurrentTaskState
from module.webui.app_dashboard import (
    _overview_execution_projection,
    _overview_task_projection,
)


@pytest.mark.parametrize(
    ("snapshot", "task", "unknown"),
    (
        (CurrentTaskSnapshot("alas", "Event"), "Event", False),
        (CurrentTaskSnapshot("alas", None, CurrentTaskState.IDLE), None, False),
        (CurrentTaskSnapshot("alas", None, CurrentTaskState.STOPPED), None, False),
        (CurrentTaskSnapshot("alas", None, CurrentTaskState.UNKNOWN), None, True),
        (None, None, True),
    ),
)
def test_dashboard_projection_collapses_idle_and_stopped_but_marks_unknown(
    snapshot: CurrentTaskSnapshot | None,
    task: str | None,
    unknown: bool,
) -> None:
    assert _overview_execution_projection(snapshot) == (task, unknown)


def test_dashboard_keeps_scheduler_pending_and_waiting_separate_from_running() -> None:
    class _Task:
        def __init__(self, command: str, next_run: str) -> None:
            self.command = command
            self.next_run = next_run

    pending = (_Task("Event", "future"),)
    waiting = (_Task("Event", "later"),)

    projection = _overview_task_projection(
        CurrentTaskSnapshot("alas", "Event"),
        pending,
        waiting,
    )

    assert projection == {
        "running": "Event",
        "running_unknown": False,
        "pending": (("Event", "future"),),
        "waiting": (("Event", "later"),),
    }
