"""Heterogeneous ordered registries for Game Settings audit and enforce."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace

import numpy as np

from module.game_settings.control_state import observe_game_setting_row_with_control_assets
from module.game_settings.definitions import (
    CUSTOM_SHIP_NAMES,
    CUSTOM_SHIP_NAMES_REQUIRED_OFF,
    DISPLAY_BATTLE_RESULT_CUTSCENE,
    DISPLAY_BATTLE_RESULT_CUTSCENE_REQUIRED_OFF,
    DISPLAY_QUICK_SWITCH_PROMPT,
    DISPLAY_QUICK_SWITCH_PROMPT_REQUIRED_OFF,
    DUPLICATE_SHIP_DISPLAY,
    DUPLICATE_SHIP_DISPLAY_REQUIRED_OFF,
    ENABLE_IDLE_SCREEN,
    ENABLE_IDLE_SCREEN_REQUIRED_OFF,
    FRAME_RATE,
    FRAME_RATE_REQUIRED_60_FPS,
    OPSI_AUTO_USE_ITEMS,
    OPSI_AUTO_USE_ITEMS_REQUIRED_ON,
    OPSI_DEFAULT_AUTO_MODE_THREAT_SAFE,
    OPSI_DEFAULT_AUTO_MODE_THREAT_SAFE_REQUIRED_OFF,
    OPSI_REDUCE_TB_GUIDANCE,
    OPSI_REDUCE_TB_GUIDANCE_REQUIRED_ON,
    STORY_AUTOPLAY,
    STORY_AUTOPLAY_REQUIRED_ENABLED,
    TEXT_AUTO_SCROLL_SPEED,
    TEXT_AUTO_SCROLL_SPEED_REQUIRED_VERY_FAST,
)
from module.game_settings.detector import detect_custom_ship_names
from module.game_settings.model import (
    FrameRateValue,
    GameSettingCheckResult,
    GameSettingChoiceCheckResult,
    GameSettingChoiceRequirement,
    GameSettingDefinition,
    GameSettingRequirement,
    GameSettingRequirementValue,
    GameSettingResult,
    GameSettingState,
    GameSettingValue,
    StoryAutoplayValue,
    TextAutoScrollSpeedValue,
)
from module.game_settings.options_detector import (
    CUSTOM_SHIP_NAMES_ROW,
    DISPLAY_BATTLE_RESULT_CUTSCENE_ROW,
    DISPLAY_QUICK_SWITCH_PROMPT_ROW,
    DUPLICATE_SHIP_DISPLAY_ROW,
    ENABLE_IDLE_SCREEN_ROW,
    FRAME_RATE_ROW,
    OPSI_AUTO_USE_ITEMS_ROW,
    OPSI_DEFAULT_AUTO_MODE_THREAT_SAFE_ROW,
    OPSI_REDUCE_TB_GUIDANCE_ROW,
    STORY_AUTOPLAY_ROW,
    TEXT_AUTO_SCROLL_SPEED_ROW,
    GameSettingRowObservation,
    GameSettingRowSpec,
)


GameSettingValueType = (
    type[GameSettingState]
    | type[FrameRateValue]
    | type[StoryAutoplayValue]
    | type[TextAutoScrollSpeedValue]
)
GameSettingDetector = Callable[[np.ndarray], GameSettingValue | None]
GameSettingObserver = Callable[[np.ndarray], GameSettingRowObservation | None]


def _row_observer(spec: GameSettingRowSpec) -> GameSettingObserver:
    def observer(image: np.ndarray) -> GameSettingRowObservation | None:
        return observe_game_setting_row_with_control_assets(image, spec)

    return observer


def _row_detector(spec: GameSettingRowSpec) -> GameSettingDetector:
    observer = _row_observer(spec)

    def detector(image: np.ndarray) -> GameSettingValue | None:
        observation = observer(image)
        if observation is None:
            return None
        return observation.value

    return detector


@dataclass(frozen=True, slots=True)
class GameSettingCheckSpec:
    """One typed audit entry and optional row observer for explicit enforce."""

    definition: GameSettingDefinition
    detector: GameSettingDetector
    requirement: GameSettingRequirementValue | None = None
    value_type: GameSettingValueType = GameSettingState
    observer: GameSettingObserver | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.definition, GameSettingDefinition):
            raise TypeError("definition должен быть GameSettingDefinition")
        if not callable(self.detector):
            raise TypeError("detector должен быть callable")
        if self.value_type not in (
            GameSettingState,
            FrameRateValue,
            StoryAutoplayValue,
            TextAutoScrollSpeedValue,
        ):
            raise TypeError("value_type должен быть поддерживаемой enum family")
        if self.observer is not None and not callable(self.observer):
            raise TypeError("observer должен быть callable или None")

        if self.requirement is None:
            return
        if not isinstance(
            self.requirement,
            (GameSettingRequirement, GameSettingChoiceRequirement),
        ):
            raise TypeError("requirement имеет неподдерживаемый тип")
        if self.requirement.definition != self.definition:
            raise ValueError("requirement относится к другой настройке")
        if type(self.requirement.expected_value) is not self.value_type:
            raise TypeError("requirement/value_type принадлежат разным value family")

    @property
    def key(self) -> str:
        return self.definition.key

    @property
    def enforce_supported(self) -> bool:
        return self.requirement is not None and self.observer is not None

    def make_result(self, value: GameSettingValue) -> GameSettingResult:
        if type(value) is not self.value_type:
            raise TypeError(
                f"Detector {self.key!r} вернул значение из другой value family"
            )
        if self.value_type is GameSettingState:
            requirement = self.requirement
            if requirement is not None and not isinstance(
                requirement,
                GameSettingRequirement,
            ):
                raise TypeError("Toggle entry получил choice requirement")
            return GameSettingCheckResult(
                definition=self.definition,
                detected_state=value,
                requirement=requirement,
            )

        requirement = self.requirement
        if requirement is not None and not isinstance(
            requirement,
            GameSettingChoiceRequirement,
        ):
            raise TypeError("Choice entry получил toggle requirement")
        return GameSettingChoiceCheckResult(
            definition=self.definition,
            detected_value=value,
            requirement=requirement,
        )

    def make_unknown_result(self) -> GameSettingResult:
        return self.make_result(self.value_type.UNKNOWN)


def build_game_settings_registry(
    entries: Iterable[GameSettingCheckSpec] = (),
    *,
    require_enforce: bool = False,
) -> tuple[GameSettingCheckSpec, ...]:
    registry = tuple(entries)
    seen_keys: set[str] = set()

    for entry in registry:
        if not isinstance(entry, GameSettingCheckSpec):
            raise TypeError("registry должен содержать GameSettingCheckSpec")
        if entry.key in seen_keys:
            raise ValueError(f"Повторяющийся ключ registry: {entry.key!r}")
        if require_enforce and entry.requirement is not None and entry.observer is None:
            raise ValueError(
                f"Required registry entry {entry.key!r} не имеет mutator observer"
            )
        seen_keys.add(entry.key)

    return registry


# Current EN uses a horizontally scrolling label for this OpSi row. The v8 live
# acceptance frame exposed a stable clipped fragment such as
# ``...uto Mode in secured Off`` after the row moved away from the viewport
# edge. Keep the full canonical aliases and add one specific inner fragment so
# OCR boxes that include the neighbouring ``Off`` text still anchor the row.
OPSI_DEFAULT_AUTO_MODE_THREAT_SAFE_PRODUCTION_ROW = replace(
    OPSI_DEFAULT_AUTO_MODE_THREAT_SAFE_ROW,
    label_aliases=(
        *OPSI_DEFAULT_AUTO_MODE_THREAT_SAFE_ROW.label_aliases,
        "Mode in secured",
    ),
)

# v10 live diagnostics proved that the lower ``Change Oathed Ship ...`` row is
# a distinct control from the earlier ``Custom Ship Names`` requirement. It may
# remain useful as a semantic navigation landmark, but it must never be accepted
# as state evidence for the production ``custom_ship_names`` key.
CUSTOM_SHIP_NAMES_PRODUCTION_ROW = replace(
    CUSTOM_SHIP_NAMES_ROW,
    label_aliases=("Custom Ship Names",),
)


# Legacy compatibility export for callers/tests that intentionally exercise the
# original single-setting preflight contract. The full production scanner uses
# GAME_SETTINGS_OPTIONS_REGISTRY below.
GAME_SETTINGS_PREFLIGHT_REGISTRY = build_game_settings_registry(
    (
        GameSettingCheckSpec(
            definition=CUSTOM_SHIP_NAMES,
            detector=detect_custom_ship_names,
            requirement=CUSTOM_SHIP_NAMES_REQUIRED_OFF,
        ),
    )
)


GAME_SETTINGS_OPTIONS_REGISTRY = build_game_settings_registry(
    (
        GameSettingCheckSpec(
            definition=FRAME_RATE,
            detector=_row_detector(FRAME_RATE_ROW),
            requirement=FRAME_RATE_REQUIRED_60_FPS,
            value_type=FrameRateValue,
            observer=_row_observer(FRAME_RATE_ROW),
        ),
        GameSettingCheckSpec(
            definition=OPSI_REDUCE_TB_GUIDANCE,
            detector=_row_detector(OPSI_REDUCE_TB_GUIDANCE_ROW),
            requirement=OPSI_REDUCE_TB_GUIDANCE_REQUIRED_ON,
            observer=_row_observer(OPSI_REDUCE_TB_GUIDANCE_ROW),
        ),
        GameSettingCheckSpec(
            definition=OPSI_AUTO_USE_ITEMS,
            detector=_row_detector(OPSI_AUTO_USE_ITEMS_ROW),
            requirement=OPSI_AUTO_USE_ITEMS_REQUIRED_ON,
            observer=_row_observer(OPSI_AUTO_USE_ITEMS_ROW),
        ),
        GameSettingCheckSpec(
            definition=OPSI_DEFAULT_AUTO_MODE_THREAT_SAFE,
            detector=_row_detector(OPSI_DEFAULT_AUTO_MODE_THREAT_SAFE_PRODUCTION_ROW),
            requirement=OPSI_DEFAULT_AUTO_MODE_THREAT_SAFE_REQUIRED_OFF,
            observer=_row_observer(OPSI_DEFAULT_AUTO_MODE_THREAT_SAFE_PRODUCTION_ROW),
        ),
        GameSettingCheckSpec(
            definition=STORY_AUTOPLAY,
            detector=_row_detector(STORY_AUTOPLAY_ROW),
            requirement=STORY_AUTOPLAY_REQUIRED_ENABLED,
            value_type=StoryAutoplayValue,
            observer=_row_observer(STORY_AUTOPLAY_ROW),
        ),
        GameSettingCheckSpec(
            definition=TEXT_AUTO_SCROLL_SPEED,
            detector=_row_detector(TEXT_AUTO_SCROLL_SPEED_ROW),
            requirement=TEXT_AUTO_SCROLL_SPEED_REQUIRED_VERY_FAST,
            value_type=TextAutoScrollSpeedValue,
            observer=_row_observer(TEXT_AUTO_SCROLL_SPEED_ROW),
        ),
        GameSettingCheckSpec(
            definition=ENABLE_IDLE_SCREEN,
            detector=_row_detector(ENABLE_IDLE_SCREEN_ROW),
            requirement=ENABLE_IDLE_SCREEN_REQUIRED_OFF,
            observer=_row_observer(ENABLE_IDLE_SCREEN_ROW),
        ),
        GameSettingCheckSpec(
            definition=DUPLICATE_SHIP_DISPLAY,
            detector=_row_detector(DUPLICATE_SHIP_DISPLAY_ROW),
            requirement=DUPLICATE_SHIP_DISPLAY_REQUIRED_OFF,
            observer=_row_observer(DUPLICATE_SHIP_DISPLAY_ROW),
        ),
        GameSettingCheckSpec(
            definition=DISPLAY_QUICK_SWITCH_PROMPT,
            detector=_row_detector(DISPLAY_QUICK_SWITCH_PROMPT_ROW),
            requirement=DISPLAY_QUICK_SWITCH_PROMPT_REQUIRED_OFF,
            observer=_row_observer(DISPLAY_QUICK_SWITCH_PROMPT_ROW),
        ),
        GameSettingCheckSpec(
            definition=DISPLAY_BATTLE_RESULT_CUTSCENE,
            detector=_row_detector(DISPLAY_BATTLE_RESULT_CUTSCENE_ROW),
            requirement=DISPLAY_BATTLE_RESULT_CUTSCENE_REQUIRED_OFF,
            observer=_row_observer(DISPLAY_BATTLE_RESULT_CUTSCENE_ROW),
        ),
        GameSettingCheckSpec(
            definition=CUSTOM_SHIP_NAMES,
            detector=_row_detector(CUSTOM_SHIP_NAMES_PRODUCTION_ROW),
            requirement=CUSTOM_SHIP_NAMES_REQUIRED_OFF,
            observer=_row_observer(CUSTOM_SHIP_NAMES_PRODUCTION_ROW),
        ),
    ),
    require_enforce=True,
)

GAME_SETTINGS_PRODUCTION_KEYS = tuple(
    entry.key for entry in GAME_SETTINGS_OPTIONS_REGISTRY
)
