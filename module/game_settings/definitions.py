"""Canonical Settings -> Options requirements for Game Settings preflight."""

from module.game_settings.model import (
    FrameRateValue,
    GameSettingChoiceRequirement,
    GameSettingDefinition,
    GameSettingRequirement,
    GameSettingState,
    StoryAutoplayValue,
    TextAutoScrollSpeedValue,
)


FRAME_RATE = GameSettingDefinition("frame_rate", "options")
OPSI_REDUCE_TB_GUIDANCE = GameSettingDefinition(
    "opsi_reduce_tb_guidance",
    "options",
)
OPSI_AUTO_USE_ITEMS = GameSettingDefinition("opsi_auto_use_items", "options")
OPSI_DEFAULT_AUTO_MODE_THREAT_SAFE = GameSettingDefinition(
    "opsi_default_auto_mode_threat_safe",
    "options",
)
STORY_AUTOPLAY = GameSettingDefinition("story_autoplay", "options")
TEXT_AUTO_SCROLL_SPEED = GameSettingDefinition(
    "text_auto_scroll_speed",
    "options",
)

# Source reconciliation:
# - current ALAS README_en still says "No Sleep Mode on Main Menu = Off";
# - current ALAS README.md replaced the former main-menu keep-awake requirement
#   with "Enable Idle Screen = Off" in 2024-07;
# - current direct upstream wess09/AzurPilot README.en.md also requires
#   "Idle mode settings, enable idle mode = Off".
# This fork targets current EN AzurPilot UI, so the direct-upstream/current-UI
# requirement is authoritative. We intentionally do NOT enforce both rows.
ENABLE_IDLE_SCREEN = GameSettingDefinition("enable_idle_screen", "options")

DUPLICATE_SHIP_DISPLAY = GameSettingDefinition(
    "duplicate_ship_display",
    "options",
)
DISPLAY_QUICK_SWITCH_PROMPT = GameSettingDefinition(
    "display_quick_switch_prompt",
    "options",
)
DISPLAY_BATTLE_RESULT_CUTSCENE = GameSettingDefinition(
    "display_battle_result_cutscene",
    "options",
)
CUSTOM_SHIP_NAMES = GameSettingDefinition("custom_ship_names", "options")


FRAME_RATE_REQUIRED_60_FPS = GameSettingChoiceRequirement(
    FRAME_RATE,
    FrameRateValue.FPS_60,
)
OPSI_REDUCE_TB_GUIDANCE_REQUIRED_ON = GameSettingRequirement(
    OPSI_REDUCE_TB_GUIDANCE,
    GameSettingState.ON,
)
OPSI_AUTO_USE_ITEMS_REQUIRED_ON = GameSettingRequirement(
    OPSI_AUTO_USE_ITEMS,
    GameSettingState.ON,
)
OPSI_DEFAULT_AUTO_MODE_THREAT_SAFE_REQUIRED_OFF = GameSettingRequirement(
    OPSI_DEFAULT_AUTO_MODE_THREAT_SAFE,
    GameSettingState.OFF,
)
STORY_AUTOPLAY_REQUIRED_ENABLED = GameSettingChoiceRequirement(
    STORY_AUTOPLAY,
    StoryAutoplayValue.ENABLED,
)
TEXT_AUTO_SCROLL_SPEED_REQUIRED_VERY_FAST = GameSettingChoiceRequirement(
    TEXT_AUTO_SCROLL_SPEED,
    TextAutoScrollSpeedValue.VERY_FAST,
)
ENABLE_IDLE_SCREEN_REQUIRED_OFF = GameSettingRequirement(
    ENABLE_IDLE_SCREEN,
    GameSettingState.OFF,
)
DUPLICATE_SHIP_DISPLAY_REQUIRED_OFF = GameSettingRequirement(
    DUPLICATE_SHIP_DISPLAY,
    GameSettingState.OFF,
)
DISPLAY_QUICK_SWITCH_PROMPT_REQUIRED_OFF = GameSettingRequirement(
    DISPLAY_QUICK_SWITCH_PROMPT,
    GameSettingState.OFF,
)
DISPLAY_BATTLE_RESULT_CUTSCENE_REQUIRED_OFF = GameSettingRequirement(
    DISPLAY_BATTLE_RESULT_CUTSCENE,
    GameSettingState.OFF,
)
CUSTOM_SHIP_NAMES_REQUIRED_OFF = GameSettingRequirement(
    CUSTOM_SHIP_NAMES,
    GameSettingState.OFF,
)
