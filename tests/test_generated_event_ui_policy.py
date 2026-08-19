from types import ModuleType

import pytest

from campaign import _apply_generated_campaign_ui_policy


def _module_with_config(config_class: type) -> ModuleType:
    module = ModuleType("test_generated_event_ui_policy")
    module.Config = config_class
    return module


def test_20241219_layout_preserves_explicit_map_mode_switch():
    class Config:
        MAP_HAS_MODE_SWITCH = False

    module = _module_with_config(Config)

    _apply_generated_campaign_ui_policy(module, "20241219")

    assert Config.MAP_HAS_MODE_SWITCH is False
    assert Config.STAGE_ENTRANCE == ["half", "20240725"]
    assert Config.MAP_CHAPTER_SWITCH_20241219 is True


def test_20241219_layout_keeps_mode_switch_as_default():
    class Config:
        pass

    module = _module_with_config(Config)

    _apply_generated_campaign_ui_policy(module, "20241219")

    assert Config.MAP_HAS_MODE_SWITCH is True


def test_20260326_layout_selects_only_its_chapter_switch():
    class Config:
        pass

    module = _module_with_config(Config)

    _apply_generated_campaign_ui_policy(module, "20260326")

    assert Config.MAP_CHAPTER_SWITCH_20241219 is False
    assert Config.MAP_CHAPTER_SWITCH_20241219_SP is False
    assert Config.MAP_CHAPTER_SWITCH_20241219_SPEX is False
    assert Config.MAP_CHAPTER_SWITCH_20260326 is True


@pytest.mark.parametrize("layout", [None, "legacy"])
def test_legacy_or_missing_layout_leaves_config_unchanged(layout):
    class Config:
        MARKER = "unchanged"

    module = _module_with_config(Config)
    before = dict(vars(Config))

    _apply_generated_campaign_ui_policy(module, layout)

    assert dict(vars(Config)) == before


def test_unknown_layout_fails_closed():
    class Config:
        pass

    module = _module_with_config(Config)

    with pytest.raises(ValueError, match="Неподдерживаемая раскладка"):
        _apply_generated_campaign_ui_policy(module, "unknown")
