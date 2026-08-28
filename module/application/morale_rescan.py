"""Политика периодической сверки per-ship morale на безопасной границе карты."""

from __future__ import annotations

from dataclasses import dataclass

_POLICY_STORAGE_KEY = "MoraleRescan"


def _non_negative_int(raw: object, *, name: str, default: int) -> int:
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        raise ValueError(f"{name} должен быть целым числом >= 0")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} должен быть целым числом >= 0") from exc
    if value < 0:
        raise ValueError(f"{name} должен быть целым числом >= 0")
    return value


@dataclass(frozen=True, slots=True)
class MoraleRescanPolicy:
    """Политика из task config; `0` отключает соответствующий trigger."""

    runs: int = 10
    minutes: int = 60

    def __post_init__(self) -> None:
        if type(self.runs) is not int or self.runs < 0:
            raise ValueError("runs должен быть int >= 0")
        if type(self.minutes) is not int or self.minutes < 0:
            raise ValueError("minutes должен быть int >= 0")

    @classmethod
    def from_config(cls, config) -> "MoraleRescanPolicy":
        """Прочитать cadence из штатного per-task `Storage.Storage`."""

        storage = getattr(config, "Storage_Storage", None)
        if storage is None:
            raw_policy = {}
        elif not isinstance(storage, dict):
            raise ValueError("Storage.Storage должен быть объектом")
        else:
            raw_policy = storage.get(_POLICY_STORAGE_KEY, {})
        if raw_policy is None:
            raw_policy = {}
        if not isinstance(raw_policy, dict):
            raise ValueError("Storage.Storage.MoraleRescan должен быть объектом")

        return cls(
            runs=_non_negative_int(
                raw_policy.get("Runs"),
                name="Storage.Storage.MoraleRescan.Runs",
                default=10,
            ),
            minutes=_non_negative_int(
                raw_policy.get("Minutes"),
                name="Storage.Storage.MoraleRescan.Minutes",
                default=60,
            ),
        )

    def due(
        self,
        *,
        completed_runs: int,
        elapsed_seconds: float,
    ) -> tuple[bool, str | None]:
        if type(completed_runs) is not int or completed_runs < 0:
            raise ValueError("completed_runs должен быть int >= 0")
        if not isinstance(elapsed_seconds, (int, float)) or elapsed_seconds < 0:
            raise ValueError("elapsed_seconds должен быть >= 0")
        due_runs = (
            self.runs > 0
            and completed_runs > 0
            and completed_runs % self.runs == 0
        )
        due_time = self.minutes > 0 and elapsed_seconds >= self.minutes * 60
        if due_runs and due_time:
            return True, "runs+time"
        if due_runs:
            return True, "runs"
        if due_time:
            return True, "time"
        return False, None


__all__ = ("MoraleRescanPolicy",)
