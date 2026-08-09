"""Декларативный registry read-only preflight-проверок Game Settings.

Stage 6 registry описывает только текущую tri-state family, где detector
возвращает ``ON``, ``OFF``, ``UNKNOWN`` либо ``None`` при отсутствии строки в
текущем viewport. Будущие discrete settings вроде FPS или autoplay speed не
должны кодироваться через эту модель как фиктивные ON/OFF значения.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

import numpy as np

from module.game_settings.definitions import (
    CUSTOM_SHIP_NAMES,
    CUSTOM_SHIP_NAMES_REQUIRED_OFF,
)
from module.game_settings.detector import detect_custom_ship_names
from module.game_settings.model import (
    GameSettingDefinition,
    GameSettingRequirement,
    GameSettingState,
)


GameSettingDetector = Callable[[np.ndarray], GameSettingState | None]
"""Read-only detector одного stable frame текущего Options viewport."""


@dataclass(frozen=True, slots=True)
class GameSettingCheckSpec:
    """Immutable tri-state preflight entry: definition + requirement + detector."""

    definition: GameSettingDefinition
    detector: GameSettingDetector
    requirement: GameSettingRequirement | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.definition, GameSettingDefinition):
            raise TypeError("definition должен быть GameSettingDefinition")
        if not callable(self.detector):
            raise TypeError("detector должен быть callable")
        if self.requirement is not None:
            if not isinstance(self.requirement, GameSettingRequirement):
                raise TypeError("requirement должен быть GameSettingRequirement или None")
            if self.requirement.definition != self.definition:
                raise ValueError("requirement относится к другой настройке")
            if self.requirement.expected_state is GameSettingState.UNKNOWN:
                raise ValueError("UNKNOWN нельзя использовать как требуемое состояние")

    @property
    def key(self) -> str:
        """Вернуть стабильный key из definition."""
        return self.definition.key


def build_game_settings_registry(
    entries: Iterable[GameSettingCheckSpec] = (),
) -> tuple[GameSettingCheckSpec, ...]:
    """Зафиксировать ordered registry и fail-fast проверить duplicate keys."""

    registry = tuple(entries)
    seen_keys: set[str] = set()

    for entry in registry:
        if not isinstance(entry, GameSettingCheckSpec):
            raise TypeError("registry должен содержать GameSettingCheckSpec")
        if entry.key in seen_keys:
            raise ValueError(f"Повторяющийся ключ registry: {entry.key!r}")
        seen_keys.add(entry.key)

    return registry


GAME_SETTINGS_PREFLIGHT_REGISTRY = build_game_settings_registry(
    (
        GameSettingCheckSpec(
            definition=CUSTOM_SHIP_NAMES,
            detector=detect_custom_ship_names,
            requirement=CUSTOM_SHIP_NAMES_REQUIRED_OFF,
        ),
    )
)
"""Stage 6 production registry: только существующая Custom Ship Names check."""
