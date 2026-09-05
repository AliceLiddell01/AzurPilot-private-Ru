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
    state = reader.read_state("alas", ("DailyTask", "WeeklyTask"))
    assert state["WeeklyTask"].as_dict()["next_run"] == "2026-09-04T00:30:00+00:00"


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


def test_scheduler_reader_accepts_registered_unicode_and_spaced_names(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    profile = "Профиль один"
    task = "Ежедневная задача"
    (config / f"{profile}.json").write_text(
        json.dumps(
            {
                task: {
                    "Scheduler": {
                        "Enable": True,
                        "NextRun": "2026-09-04T00:30:00+00:00",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    queue = SchedulerRuntimeStateReader(tmp_path).read_queue(profile, (task,))

    assert [entry.task for entry in queue] == [task]


def test_scheduler_reader_skips_disabled_task_without_next_run(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "alas.json").write_text(
        json.dumps(
            {
                "DisabledTask": {"Scheduler": {"Enable": False}},
            }
        ),
        encoding="utf-8",
    )

    reader = SchedulerRuntimeStateReader(tmp_path)

    assert reader.read_state("alas", ("DisabledTask",)) == {}
    assert reader.read_queue("alas", ("DisabledTask",)) == ()


@pytest.mark.parametrize(
    "scheduler",
    (
        {"NextRun": "2026-09-04T00:30:00+00:00"},
        {"Enable": True},
    ),
)
def test_scheduler_reader_requires_enable_and_next_run_fields(
    tmp_path: Path,
    scheduler: dict[str, object],
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "alas.json").write_text(
        json.dumps({"DailyTask": {"Scheduler": scheduler}}),
        encoding="utf-8",
    )

    with pytest.raises(SchedulerRuntimeStateError) as error:
        SchedulerRuntimeStateReader(tmp_path).read_state("alas", ("DailyTask",))

    assert error.value.code == "SCHEDULER_STATE_MISSING"
