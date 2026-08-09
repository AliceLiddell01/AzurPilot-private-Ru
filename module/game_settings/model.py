"""Чистая доменная модель результатов проверки игровых настроек."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import Enum


_IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class GameSettingState(Enum):
    """Достоверно обнаруженное состояние настройки или его неизвестность."""

    ON = "on"
    OFF = "off"
    UNKNOWN = "unknown"

    def __bool__(self) -> bool:
        """Запретить двусмысленное неявное преобразование состояния в bool."""
        raise TypeError("GameSettingState нельзя неявно преобразовать в bool")


def _validate_identifier(value: object, *, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} должен быть строкой")
    if _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ValueError(
            f"{field_name} должен быть непустым стабильным идентификатором "
            "в нижнем регистре"
        )


@dataclass(frozen=True, slots=True)
class GameSettingDefinition:
    """Стабильная идентичность игровой настройки и место её расположения."""

    key: str
    location: str

    def __post_init__(self) -> None:
        _validate_identifier(self.key, field_name="key")
        _validate_identifier(self.location, field_name="location")


@dataclass(frozen=True, slots=True)
class GameSettingRequirement:
    """Требуемое рабочее состояние конкретной игровой настройки."""

    definition: GameSettingDefinition
    expected_state: GameSettingState

    def __post_init__(self) -> None:
        if not isinstance(self.definition, GameSettingDefinition):
            raise TypeError("definition должен быть GameSettingDefinition")
        if not isinstance(self.expected_state, GameSettingState):
            raise TypeError("expected_state должен быть GameSettingState")
        if self.expected_state is GameSettingState.UNKNOWN:
            raise ValueError("UNKNOWN нельзя использовать как требуемое состояние")


@dataclass(frozen=True, slots=True)
class GameSettingCheckResult:
    """Результат определения одной настройки и проверки её требования."""

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
        """Вернуть стабильный ключ настройки."""
        return self.definition.key

    @property
    def expected_state(self) -> GameSettingState | None:
        """Вернуть требуемое состояние или None для информационной проверки."""
        if self.requirement is None:
            return None
        return self.requirement.expected_state

    @property
    def is_required(self) -> bool:
        """Показать, задано ли для результата обязательное состояние."""
        return self.requirement is not None

    @property
    def compatible(self) -> bool | None:
        """Проверить требование или вернуть None, когда требования нет."""
        expected_state = self.expected_state
        if expected_state is None:
            return None
        return self.detected_state is expected_state


@dataclass(frozen=True, slots=True, init=False)
class GameSettingsScanResult:
    """Упорядоченный неизменяемый набор результатов без повторяющихся ключей."""

    results: tuple[GameSettingCheckResult, ...]

    def __init__(self, results: Iterable[GameSettingCheckResult] = ()) -> None:
        resolved_results = tuple(results)
        seen_keys: set[str] = set()

        for result in resolved_results:
            if not isinstance(result, GameSettingCheckResult):
                raise TypeError("results должен содержать GameSettingCheckResult")
            if result.key in seen_keys:
                raise ValueError(f"Повторяющийся ключ настройки: {result.key!r}")
            seen_keys.add(result.key)

        object.__setattr__(self, "results", resolved_results)

    def __iter__(self) -> Iterator[GameSettingCheckResult]:
        return iter(self.results)

    def __len__(self) -> int:
        return len(self.results)

    def get(self, key: str) -> GameSettingCheckResult | None:
        """Найти результат по стабильному ключу без изменения порядка."""
        for result in self.results:
            if result.key == key:
                return result
        return None

    @property
    def required(self) -> tuple[GameSettingCheckResult, ...]:
        """Вернуть результаты, для которых задано обязательное состояние."""
        return tuple(result for result in self.results if result.is_required)

    @property
    def unknown(self) -> tuple[GameSettingCheckResult, ...]:
        """Вернуть результаты с недостоверно определённым состоянием."""
        return tuple(
            result
            for result in self.results
            if result.detected_state is GameSettingState.UNKNOWN
        )

    @property
    def incompatible(self) -> tuple[GameSettingCheckResult, ...]:
        """Вернуть обязательные результаты, которые не прошли требование."""
        return tuple(result for result in self.required if result.compatible is False)

    @property
    def all_required_compatible(self) -> bool | None:
        """Вернуть общий итог или None, если обязательных требований нет."""
        required = self.required
        if not required:
            return None
        return all(result.compatible is True for result in required)
