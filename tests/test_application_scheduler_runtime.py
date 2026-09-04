from __future__ import annotations

import json
from pathlib import Path

import pytest

from module.application.scheduler_runtime import (
    SchedulerRuntimeStateError,
    SchedulerRuntimeStateReader,
)


def test_scheduler_reader_uses_persisted_enable_value_without_normalizing_it(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "alas.json").write_text(
        json.dumps(
            {
                "DailyTask": {
                    "Scheduler": {
                        "Enable": False,
                        "NextRun": "2026-09-04T01:00:00+00:00",
                    }
                },
                "WeeklyTask": {
                    "Scheduler": {
                        "Enable": True,
                        "NextRun": "2026-09-04T00:30:00+00:00",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    reader = SchedulerRuntimeStateReader(tmp_path)
    assert reader.semantic_fingerprint("alas", ("DailyTask", "WeeklyTask")) == (
        ("DailyTask", False, "2026-09-04T01:00:00+00:00"),
        ("WeeklyTask", True, "2026-09-04T00:30:00+00:00"),
    )
    queue = reader.read_queue(
        "alas",
        ("DailyTask", "WeeklyTask"),
    )
    assert [entry.task for entry in queue] == ["WeeklyTask"]
    assert queue[0].next_run.isoformat() == "2026-09-04T00:30:00+00:00"


def test_scheduler_reader_rejects_unsafe_paths_and_malformed_persisted_state(tmp_path: Path) -> None:
    reader = SchedulerRuntimeStateReader(tmp_path)
    with pytest.raises(SchedulerRuntimeStateError) as profile_error:
        reader.read_state("../outside", ())
    assert profile_error.value.code == "SCHEDULER_STATE_PATH_INVALID"

    config = tmp_path / "config"
    config.mkdir()
    (config / "alas.json").write_text(
        json.dumps({"DailyTask": {"Scheduler": {"Enable": "false"}}}),
        encoding="utf-8",
    )
    with pytest.raises(SchedulerRuntimeStateError) as state_error:
        reader.read_state("alas", ("DailyTask",))
    assert state_error.value.code == "SCHEDULER_STATE_INVALID"
