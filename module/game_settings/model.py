"""Типизированная доменная модель Game Settings audit/enforcement."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import Enum


_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class GameSettingKind(Enum):
    TOGGLE = "toggle"
    CHOICE = "choice"


class GameSettingState(Enum):
    ON = "on"
    OFF = "off"
    UNKNOWN = "unknown"

    def __bool__(self) -> bool:
        raise TypeError("GameSettingState нельзя неявно преобразовывать в bool")


class FrameRateValue(Enum):
    FPS_30 = "30_fps"
    FPS_60 = "60_fps"
    UNKNOWN = "unknown"

    def __bool__(self) -> bool:
        raise TypeError("FrameRateValue нельзя неявно преобразовывать в bool")


class StoryAutoplayValue(Enum):
    DISABLED = "disabled"
    ENABLED = "enabled"
    UNKNOWN = "unknown"

    def __bool__(self) -> bool:
        raise TypeError("StoryAutoplayValue нельзя неявно преобразовывать в bool")


class TextAutoScrollSpeedValue(Enum):
    SLOW = "slow"
    NORMAL = "normal"
    FAST = "fast"
    VERY_FAST = "very_fast"
    UNKNOWN = "unknown"

    def __bool__(self) -> bool:
        raise TypeError(
            "TextAutoScrollSpeedValue нельзя неявно преобразовывать в bool"
        )


GameSettingChoiceValue = (
    FrameRateValue | StoryAutoplayValue | TextAutoScrollSpeedValue
)
GameSettingValue = GameSettingState | GameSettingChoiceValue


def is_unknown_game_setting_value(value: GameSettingValue) -> bool:
    return value.value == "unknown"


def game_setting_value_kind(value: GameSettingValue) -> GameSettingKind:
    if isinstance(value, GameSettingState):
        return GameSettingKind.TOGGLE
    if isinstance(
        value,
        (FrameRateValue, StoryAutoplayValue, TextAutoScrollSpeedValue),
    ):
        return GameSettingKind.CHOICE
    raise TypeError("Неподдерживаемый тип значения Game Setting")


@dataclass(frozen=True, slots=True)
class GameSettingDefinition:
    key: str
    location: str

    def __post_init__(self) -> None:
        for name, value in (("key", self.key), ("location", self.location)):
            if not isinstance(value, str):
                raise TypeError(f"{name} должен быть str")
            if not _IDENTIFIER_RE.fullmatch(value):
                raise ValueError(
                    f"{name} должен быть стабильным lowercase identifier: {value!r}"
                )


@dataclass(frozen=True, slots=True)
class GameSettingRequirement:
    definition: GameSettingDefinition
    expected_state: GameSettingState

    def __post_init__(self) -> None:
        if not isinstance(self.definition, GameSettingDefinition):
            raise TypeError("definition должен быть GameSettingDefinition")
        if not isinstance(self.expected_state, GameSettingState):
            raise TypeError("expected_state должен быть GameSettingState")
        if self.expected_state is GameSettingState.UNKNOWN:
            raise ValueError("UNKNOWN нельзя использовать как требуемое состояние")

    @property
    def expected_value(self) -> GameSettingState:
        return self.expected_state

    @property
    def kind(self) -> GameSettingKind:
        return GameSettingKind.TOGGLE


@dataclass(frozen=True, slots=True)
class GameSettingChoiceRequirement:
    definition: GameSettingDefinition
    expected_value: GameSettingChoiceValue

    def __post_init__(self) -> None:
        if not isinstance(self.definition, GameSettingDefinition):
            raise TypeError("definition должен быть GameSettingDefinition")
        if not isinstance(
            self.expected_value,
            (FrameRateValue, StoryAutoplayValue, TextAutoScrollSpeedValue),
        ):
            raise TypeError("expected_value должен быть типизированным choice enum")
        if is_unknown_game_setting_value(self.expected_value):
            raise ValueError("UNKNOWN нельзя использовать как требуемое значение")

    @property
    def kind(self) -> GameSettingKind:
        return GameSettingKind.CHOICE


GameSettingRequirementValue = GameSettingRequirement | GameSettingChoiceRequirement


@dataclass(frozen=True, slots=True)
class GameSettingCheckResult:
    definition: GameSettingDefinition
    detected_state: GameSettingState
    requirement: GameSettingRequirement | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.definition, GameSettingDefinition):
            raise TypeError("definition должен быть GameSettingDefinition")
        if not isinstance(self.detected_state, GameSettingState):
            raise TypeError("detected_state должен быть GameSettingState")
        if self.requirement is not None:
            if not isinstance(self.requirement, GameSettingRequirement):
                raise TypeError("requirement должен быть GameSettingRequirement или None")
            if self.requirement.definition != self.definition:
                raise ValueError("requirement относится к другой настройке")

    @property
    def key(self) -> str:
        return self.definition.key

    @property
    def detected_value(self) -> GameSettingState:
        return self.detected_state

    @property
    def expected_state(self) -> GameSettingState | None:
        if self.requirement is None:
            return None
        return self.requirement.expected_state

    @property
    def required_value(self) -> GameSettingState | None:
        return self.expected_state

    @property
    def kind(self) -> GameSettingKind:
        return GameSettingKind.TOGGLE

    @property
    def is_required(self) -> bool:
        return self.requirement is not None

    @property
    def compatible(self) -> bool | None:
        expected = self.expected_state
        if expected is None:
            return None
        return self.detected_state is expected


@dataclass(frozen=True, slots=True)
class GameSettingChoiceCheckResult:
    definition: GameSettingDefinition
    detected_value: GameSettingChoiceValue
    requirement: GameSettingChoiceRequirement | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.definition, GameSettingDefinition):
            raise TypeError("definition должен быть GameSettingDefinition")
        if not isinstance(
            self.detected_value,
            (FrameRateValue, StoryAutoplayValue, TextAutoScrollSpeedValue),
        ):
            raise TypeError("detected_value должен быть типизированным choice enum")
        if self.requirement is not None:
            if not isinstance(self.requirement, GameSettingChoiceRequirement):
                raise TypeError(
                    "requirement должен быть GameSettingChoiceRequirement или None"
                )
            if self.requirement.definition != self.definition:
                raise ValueError("requirement относится к другой настройке")
            if type(self.requirement.expected_value) is not type(self.detected_value):
                raise TypeError("detected/required choice принадлежат разным value family")

    @property
    def key(self) -> str:
        return self.definition.key

    @property
    def required_value(self) -> GameSettingChoiceValue | None:
        if self.requirement is None:
            return None
        return self.requirement.expected_value

    @property
    def kind(self) -> GameSettingKind:
        return GameSettingKind.CHOICE

    @property
    def is_required(self) -> bool:
        return self.requirement is not None

    @property
    def compatible(self) -> bool | None:
        expected = self.required_value
        if expected is None:
            return None
        if is_unknown_game_setting_value(self.detected_value):
            return False
        return self.detected_value is expected


GameSettingResult = GameSettingCheckResult | GameSettingChoiceCheckResult


@dataclass(frozen=True, slots=True, init=False)
class GameSettingsScanResult:
    results: tuple[GameSettingResult, ...]

    def __init__(self, results: Iterable[GameSettingResult] = ()) -> None:
        frozen_results = tuple(results)
        seen: set[str] = set()
        for result in frozen_results:
            if not isinstance(
                result,
                (GameSettingCheckResult, GameSettingChoiceCheckResult),
            ):
                raise TypeError(
                    "results должен содержать GameSettingCheckResult "
                    "или GameSettingChoiceCheckResult"
                )
            if result.key in seen:
                raise ValueError(f"Повторяющийся ключ результата: {result.key!r}")
            seen.add(result.key)
        object.__setattr__(self, "results", frozen_results)

    def __iter__(self) -> Iterator[GameSettingResult]:
        return iter(self.results)

    def __len__(self) -> int:
        return len(self.results)

    def get(self, key: str) -> GameSettingResult | None:
        for result in self.results:
            if result.key == key:
                return result
        return None

    @property
    def required(self) -> tuple[GameSettingResult, ...]:
        return tuple(result for result in self.results if result.is_required)

    @property
    def unknown(self) -> tuple[GameSettingResult, ...]:
        return tuple(
            result
            for result in self.results
            if is_unknown_game_setting_value(result.detected_value)
        )

    @property
    def incompatible(self) -> tuple[GameSettingResult, ...]:
        return tuple(
            result for result in self.required if result.compatible is False
        )

    @property
    def all_required_compatible(self) -> bool | None:
        required = self.required
        if not required:
            return None
        return all(result.compatible is True for result in required)


@dataclass(frozen=True, slots=True)
class GameSettingAppliedChange:
    key: str
    before: GameSettingValue
    after: GameSettingValue
    verified: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not _IDENTIFIER_RE.fullmatch(self.key):
            raise ValueError(f"Недопустимый key изменения: {self.key!r}")
        for name, value in (("before", self.before), ("after", self.after)):
            if not isinstance(
                value,
                (
                    GameSettingState,
                    FrameRateValue,
                    StoryAutoplayValue,
                    TextAutoScrollSpeedValue,
                ),
            ):
                raise TypeError(
                    f"{name} должен быть типизированным Game Setting value"
                )
        if type(self.before) is not type(self.after):
            raise TypeError("before/after должны принадлежать одной value family")
        if self.verified and is_unknown_game_setting_value(self.after):
            raise ValueError("verified change не может завершаться значением UNKNOWN")


@dataclass(frozen=True, slots=True)
class GameSettingsEnforcementResult:
    before: GameSettingsScanResult
    changes: tuple[GameSettingAppliedChange, ...] = field(default_factory=tuple)
    after: GameSettingsScanResult | None = None
    success: bool = False
    blocked_reason: str | None = None
    failed_key: str | None = None
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.before, GameSettingsScanResult):
            raise TypeError("before должен быть GameSettingsScanResult")
        if self.after is not None and not isinstance(
            self.after, GameSettingsScanResult
        ):
            raise TypeError("after должен быть GameSettingsScanResult или None")
        changes = tuple(self.changes)
        if not all(isinstance(change, GameSettingAppliedChange) for change in changes):
            raise TypeError("changes должен содержать GameSettingAppliedChange")
        object.__setattr__(self, "changes", changes)
        if self.success and (
            self.blocked_reason is not None
            or self.failed_key is not None
            or self.failure_reason is not None
        ):
            raise ValueError("success result не может одновременно содержать failure")
        if self.blocked_reason is not None and (
            self.failed_key is not None or self.failure_reason is not None
        ):
            raise ValueError("blocked и operational failure взаимоисключающие")
        if (
            not self.success
            and self.blocked_reason is None
            and self.failure_reason is None
        ):
            raise ValueError("Неуспешный enforcement result должен содержать причину")

    @property
    def changed_keys(self) -> tuple[str, ...]:
        return tuple(change.key for change in self.changes)

    @property
    def blocked(self) -> bool:
        return self.blocked_reason is not None
