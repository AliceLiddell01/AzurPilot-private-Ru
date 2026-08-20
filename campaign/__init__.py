"""Ленивый мост совместимости для сгенерированных карт события.

Обычный ``import campaign`` не читает реестр событий и не исполняет карты.
Разрешение выполняется только при попытке импортировать конкретный этап через
закреплённый в Event registry selector.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import sys
from functools import wraps
from types import ModuleType

from module.event_datamine.campaign_selector import (
    generated_campaign_ui_layout,
    generated_stage_navigation_for_module,
    resolve_generated_campaign_module,
)
from module.exception import CampaignNameError
from module.logger import logger


def _apply_generated_campaign_ui_policy(module: ModuleType, layout: str | None) -> None:
    """Применить проверенную раскладку интерфейса к сгенерированной карте."""

    config_class = getattr(module, "Config", None)
    if config_class is None or not layout or layout == "legacy":
        return
    if layout == "20241219":
        config_class.MAP_CHAPTER_SWITCH_20241219 = True
        config_class.MAP_CHAPTER_SWITCH_20241219_SP = False
        config_class.MAP_CHAPTER_SWITCH_20241219_SPEX = False
        config_class.MAP_CHAPTER_SWITCH_20260326 = False
        # Современная раскладка 20241219 использует те же визуальные входы этапов,
        # что и карты upstream: состояние ``half`` и шаблон входа 20240725.
        # Это контракт раскладки, а не признак конкретного события.
        config_class.STAGE_ENTRANCE = ["half", "20240725"]
        # Раскладка включает переключатель сложности по умолчанию, но явный факт
        # конкретной карты имеет больший приоритет. Одноразовый SP, например,
        # использует ту же раскладку без переключения Normal/Hard.
        if "MAP_HAS_MODE_SWITCH" not in vars(config_class):
            config_class.MAP_HAS_MODE_SWITCH = True
        return
    if layout == "20260326":
        config_class.MAP_CHAPTER_SWITCH_20241219 = False
        config_class.MAP_CHAPTER_SWITCH_20241219_SP = False
        config_class.MAP_CHAPTER_SWITCH_20241219_SPEX = False
        config_class.MAP_CHAPTER_SWITCH_20260326 = True
        return
    raise ValueError(f"Неподдерживаемая раскладка интерфейса сгенерированного события: {layout!r}")


def _apply_generated_stage_navigation_policy(module: ModuleType) -> None:
    """Перенести проверенную семантику этапа в Config канонического модуля."""

    config_class = getattr(module, "Config", None)
    if config_class is None:
        return
    navigation = generated_stage_navigation_for_module(module.__name__)
    config_class.GENERATED_EVENT_STAGE_NAVIGATION = True
    config_class.GENERATED_EVENT_AUTO_NEXT = navigation.auto_next or ""
    config_class.GENERATED_EVENT_DIFFICULTY = navigation.difficulty or ""
    config_class.GENERATED_EVENT_UI_PAGE = navigation.ui_page or ""
    config_class.GENERATED_EVENT_UI_MODE = navigation.ui_mode or ""
    config_class.GENERATED_EVENT_UI_ASIDE = navigation.ui_aside or ""
    config_class.GENERATED_EVENT_UI_CHAPTER_INDEX = navigation.ui_chapter_index or 0
    config_class.GENERATED_EVENT_ENTRANCE_NAMES = list(navigation.entrance_names)


def _generated_campaign_set_chapter(self, name, mode="normal"):
    """Перейти к generated-этапу по явному UI-маршруту runtime-policy."""

    page = self.config.GENERATED_EVENT_UI_PAGE
    ui_mode = self.config.GENERATED_EVENT_UI_MODE
    aside = self.config.GENERATED_EVENT_UI_ASIDE
    chapter_index = self.config.GENERATED_EVENT_UI_CHAPTER_INDEX
    difficulty = self.config.GENERATED_EVENT_DIFFICULTY

    if difficulty:
        self.config.override(Campaign_Mode=difficulty)

    if page == "event":
        self.ui_goto_event()
    elif page == "sp":
        self.ui_goto_sp()
    elif page == "campaign":
        self.ui_goto_campaign()
    else:
        logger.warning(f'[Кампания — UI] Неизвестная страница generated-этапа: {page!r}')
        raise CampaignNameError

    if self.config.MAP_CHAPTER_SWITCH_20260326:
        if ui_mode:
            self.campaign_ensure_mode_20241219(ui_mode)
        if aside:
            self.campaign_ensure_aside_20260326(aside)
    elif self.config.MAP_CHAPTER_SWITCH_20241219:
        if ui_mode:
            self.campaign_ensure_mode_20241219(ui_mode)
        if aside:
            self.campaign_ensure_aside_20241219(aside)
    elif ui_mode:
        self.campaign_ensure_mode(ui_mode)

    if chapter_index:
        self.campaign_ensure_chapter(chapter_index)


def _generated_campaign_get_entrance(self, name):
    """Найти вход generated-этапа по явным именам runtime-policy."""

    entrance_name = str(name)
    candidates = list(self.config.GENERATED_EVENT_ENTRANCE_NAMES)
    if not candidates:
        candidates = [str(getattr(self.MAP, "name", name) or name)]

    available = {
        str(stage_name).casefold(): stage_name
        for stage_name in self.stage_entrance
    }
    for candidate in candidates:
        key = available.get(str(candidate).casefold())
        if key is None:
            continue
        entrance = self.stage_entrance[key]
        entrance.name = entrance_name
        return entrance

    logger.warning(
        f'[Кампания — UI] Вход generated-этапа не найден: '
        f'{", ".join(str(item) for item in candidates)}'
    )
    raise CampaignNameError


def _adapt_generated_campaign_ui(module: ModuleType, ui_layout: str | None = None) -> None:
    """Настроить канонический ``MAP`` под проверенную политику интерфейса события.

    Сам класс ``Campaign`` не копируется: адаптируется тот же объект класса из
    канонического сгенерированного модуля, причём не более одного раза.
    """

    _apply_generated_campaign_ui_policy(module, ui_layout)
    _apply_generated_stage_navigation_policy(module)
    campaign_class = getattr(module, "Campaign", None)
    map_object = getattr(module, "MAP", None)
    if campaign_class is None or map_object is None:
        return
    if getattr(campaign_class, "_generated_event_ui_adapted", False):
        return
    original = getattr(campaign_class, "ensure_campaign_ui", None)
    if not callable(original):
        return

    @wraps(original)
    def ensure_campaign_ui(self, name, mode="normal", skip_first_screenshot=True):
        canonical = str(getattr(self.MAP, "name", name) or name).strip().lower()
        return original(
            self,
            canonical,
            mode=mode,
            skip_first_screenshot=skip_first_screenshot,
        )

    campaign_class.campaign_set_chapter = _generated_campaign_set_chapter
    campaign_class.campaign_get_entrance = _generated_campaign_get_entrance
    campaign_class.ensure_campaign_ui = ensure_campaign_ui
    campaign_class._generated_event_ui_adapted = True


class _GeneratedEventAliasLoader(importlib.abc.Loader):
    """Вернуть существующий канонический сгенерированный модуль без повторного исполнения."""

    def __init__(self, target: str, ui_layout: str | None = None):
        self.target = target
        self.ui_layout = ui_layout

    def create_module(self, spec):
        module = importlib.import_module(self.target)
        if self.ui_layout is None:
            # Сохраняем совместимость с тестовыми и старыми адаптерами,
            # которые принимают один аргумент.
            _adapt_generated_campaign_ui(module)
        else:
            _adapt_generated_campaign_ui(module, self.ui_layout)
        return module

    def exec_module(self, module: ModuleType) -> None:
        # Канонический модуль уже один раз исполнен через importlib.import_module().
        return None


class _GeneratedEventAliasPackageLoader(importlib.abc.Loader):
    """Создать только промежуточный package для отсутствующего legacy selector."""

    def exec_module(self, module: ModuleType) -> None:
        return None


class _GeneratedEventAliasFinder(importlib.abc.MetaPathFinder):
    """Лениво сопоставить старое имя этапа события со сгенерированным этапом."""

    def find_spec(self, fullname, path=None, target=None):
        parts = str(fullname).split(".")
        if len(parts) not in {2, 3} or parts[0] != "campaign":
            return None
        selector = parts[1]
        if not selector.startswith("event_") or selector == "event_generated":
            return None

        # Generated alias — только fallback совместимости. Реальный legacy-модуль
        # на диске всегда имеет приоритет и не должен подменяться generated-событием
        # даже при совпадающем selector из Event registry.
        if importlib.machinery.PathFinder.find_spec(fullname, path) is not None:
            return None

        if len(parts) == 2:
            # Python сначала импортирует родительский selector package и лишь затем
            # конкретный stage. Пустой synthetic package не разрешает карту сам:
            # полный stage ниже всё равно проходит существующий fail-closed resolver.
            return importlib.util.spec_from_loader(
                fullname,
                _GeneratedEventAliasPackageLoader(),
                is_package=True,
            )

        stage = parts[2]
        resolved = resolve_generated_campaign_module(
            selector,
            stage,
        )
        if resolved is None:
            return None
        ui_layout = generated_campaign_ui_layout(resolved)
        return importlib.util.spec_from_loader(
            fullname,
            _GeneratedEventAliasLoader(resolved, ui_layout=ui_layout),
        )


def _install_generated_event_alias_finder() -> None:
    """Зарегистрировать поисковик один раз без чтения реестра на старте."""

    if any(isinstance(item, _GeneratedEventAliasFinder) for item in sys.meta_path):
        return
    sys.meta_path.insert(0, _GeneratedEventAliasFinder())


_install_generated_event_alias_finder()
