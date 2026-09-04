"""Узкий authoritative reader сырого persisted scheduler state."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from module.application.game_models import SchedulerEntry

_MAX_BYTES = 1024 * 1024
_MAX_TASKS = 256
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_FALLBACK_NEXT_RUN = datetime.fromisoformat("2050-01-01")


class SchedulerRuntimeStateError(RuntimeError):
    """Сырая persisted scheduler запись небезопасна или недоступна."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SchedulerRuntimeEntry:
    task: str
    enabled: bool
    next_run: object

    def as_dict(self) -> dict[str, object]:
        return {"task": self.task, "enabled": self.enabled, "next_run": self.next_run}


class SchedulerRuntimeStateReader:
    """Читать только raw ``config/<profile>.json`` и generated task names."""

    def __init__(self, repository_root: Path | str) -> None:
        self.repository_root = Path(repository_root).resolve()
        self.config_root = self.repository_root / "config"

    def read_state(
        self,
        profile: str,
        schedulable_tasks: Sequence[str],
    ) -> dict[str, SchedulerRuntimeEntry]:
        profile = self._safe_segment(profile, field="profile")
        tasks = self._tasks(schedulable_tasks)
        path = self.config_root / f"{profile}.json"
        self._validate_path(path)
        try:
            raw = path.read_bytes()
        except FileNotFoundError as exc:
            raise SchedulerRuntimeStateError(
                "SCHEDULER_STATE_UNAVAILABLE", "Persisted scheduler profile отсутствует"
            ) from exc
        except OSError as exc:
            raise SchedulerRuntimeStateError(
                "SCHEDULER_STATE_UNREADABLE", "Persisted scheduler profile невозможно прочитать"
            ) from exc
        if len(raw) > _MAX_BYTES:
            raise SchedulerRuntimeStateError(
                "SCHEDULER_STATE_TOO_LARGE", "Persisted scheduler profile превышает допустимый размер"
            )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise SchedulerRuntimeStateError(
                "SCHEDULER_STATE_CORRUPT", "Persisted scheduler profile содержит некорректный JSON"
            ) from exc
        if not isinstance(payload, Mapping):
            raise SchedulerRuntimeStateError(
                "SCHEDULER_STATE_CORRUPT", "Persisted scheduler profile должен быть объектом"
            )

        result: dict[str, SchedulerRuntimeEntry] = {}
        for task in tasks:
            raw_task = payload.get(task)
            if not isinstance(raw_task, Mapping):
                continue
            scheduler = raw_task.get("Scheduler")
            if scheduler is None:
                continue
            if not isinstance(scheduler, Mapping):
                raise SchedulerRuntimeStateError(
                    "SCHEDULER_STATE_INVALID", f"Scheduler state задачи {task} имеет неверный тип"
                )
            enabled = scheduler.get("Enable", False)
            if type(enabled) is not bool:
                raise SchedulerRuntimeStateError(
                    "SCHEDULER_STATE_INVALID", f"Scheduler.Enable задачи {task} не является boolean"
                )
            next_run = scheduler.get("NextRun", _FALLBACK_NEXT_RUN)
            if isinstance(next_run, str):
                if len(next_run) > 80:
                    raise SchedulerRuntimeStateError(
                        "SCHEDULER_STATE_INVALID", f"Scheduler.NextRun задачи {task} слишком длинный"
                    )
                try:
                    next_run = datetime.fromisoformat(next_run)
                except ValueError as exc:
                    raise SchedulerRuntimeStateError(
                        "SCHEDULER_STATE_INVALID", f"Scheduler.NextRun задачи {task} имеет неверный формат"
                    ) from exc
            elif not isinstance(next_run, datetime):
                raise SchedulerRuntimeStateError(
                    "SCHEDULER_STATE_INVALID", f"Scheduler.NextRun задачи {task} имеет неверный тип"
                )
            result[task] = SchedulerRuntimeEntry(task, enabled, next_run)
        return result

    def read_queue(
        self,
        profile: str,
        schedulable_tasks: Sequence[str],
    ) -> tuple[SchedulerEntry, ...]:
        state = self.read_state(profile, schedulable_tasks)
        entries = tuple(
            SchedulerEntry(task=entry.task, next_run=entry.next_run)
            for entry in state.values()
            if entry.enabled
        )
        return tuple(
            sorted(
                entries,
                key=lambda entry: (
                    0,
                    _datetime_timestamp(entry.next_run),
                    entry.task,
                ),
            )
        )

    def semantic_fingerprint(
        self,
        profile: str,
        schedulable_tasks: Sequence[str],
    ) -> tuple[tuple[str, bool, str], ...]:
        """Вернуть persisted scheduler semantics для before/after проверки."""

        state = self.read_state(profile, schedulable_tasks)
        return tuple(
            (
                task,
                entry.enabled,
                entry.next_run.isoformat()
                if isinstance(entry.next_run, datetime)
                else str(entry.next_run),
            )
            for task, entry in sorted(state.items())
        )

    @staticmethod
    def _tasks(values: Sequence[str]) -> tuple[str, ...]:
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise SchedulerRuntimeStateError(
                "SCHEDULER_TASK_REGISTRY_INVALID", "Generated scheduler registry имеет неверный тип"
            )
        if len(values) > _MAX_TASKS:
            raise SchedulerRuntimeStateError(
                "SCHEDULER_TASK_REGISTRY_INVALID", "Generated scheduler registry слишком большой"
            )
        result = []
        for value in values:
            result.append(SchedulerRuntimeStateReader._safe_segment(value, field="task"))
        if len(result) != len(set(result)):
            raise SchedulerRuntimeStateError(
                "SCHEDULER_TASK_REGISTRY_INVALID", "Generated scheduler registry содержит дубликаты"
            )
        return tuple(result)

    @staticmethod
    def _safe_segment(value: object, *, field: str) -> str:
        if not isinstance(value, str) or _SAFE_SEGMENT.fullmatch(value) is None or ".." in value:
            raise SchedulerRuntimeStateError(
                "SCHEDULER_STATE_PATH_INVALID", f"{field} имеет небезопасный формат"
            )
        return value

    def _validate_path(self, path: Path) -> None:
        if self.config_root.is_symlink() or path.is_symlink() or bool(
            getattr(self.config_root, "is_junction", lambda: False)()
        ) or bool(getattr(path, "is_junction", lambda: False)()):
            raise SchedulerRuntimeStateError(
                "SCHEDULER_STATE_UNSAFE_PATH", "Persisted scheduler path не должен быть ссылкой"
            )
        try:
            path.resolve().relative_to(self.config_root.resolve())
        except (OSError, RuntimeError, ValueError) as exc:
            raise SchedulerRuntimeStateError(
                "SCHEDULER_STATE_UNSAFE_PATH", "Persisted scheduler path выходит за config root"
            ) from exc


def _datetime_timestamp(value: object) -> float:
    if not isinstance(value, datetime):
        return math.inf
    try:
        return value.timestamp()
    except (OverflowError, OSError, ValueError):
        return math.inf


__all__ = [
    "SchedulerRuntimeEntry",
    "SchedulerRuntimeStateError",
    "SchedulerRuntimeStateReader",
]
