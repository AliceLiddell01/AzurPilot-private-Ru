"""Навигационный граф Game Settings Scanner.

Страницы регистрируются в общем ``Page``-графе только при импорте подсистемы.
``page_settings`` означает оболочку Settings на любом разделе, кроме Options:
её check marker — неизменный невыбранный пункт Options в sidebar.
"""

from module.game_settings.assets import (
    GAME_SETTINGS_MAIN_GOTO_SETTINGS,
    GAME_SETTINGS_OPTIONS_SELECTED,
    GAME_SETTINGS_OPTIONS_UNSELECTED,
)
from module.ui.assets import GOTO_MAIN
from module.ui.page import Page, page_main, page_main_white


page_settings = Page(GAME_SETTINGS_OPTIONS_UNSELECTED)
page_settings_options = Page(GAME_SETTINGS_OPTIONS_SELECTED)

# Пользовательский current/new Main UI подтверждён реальным 1280x720 screenshot.
page_main_white.link(button=GAME_SETTINGS_MAIN_GOTO_SETTINGS, destination=page_settings)

# Любой Settings tab с невыбранным Options -> Options.
page_settings.link(
    button=GAME_SETTINGS_OPTIONS_UNSELECTED,
    destination=page_settings_options,
)

# Settings и Options используют штатную Home-кнопку проекта.
page_settings.link(button=GOTO_MAIN, destination=page_main)
page_settings_options.link(button=GOTO_MAIN, destination=page_main)
