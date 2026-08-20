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
    resolve_generated_campaign_module,
)
from module.event_datamine.stage_navigation import (
    generated_stage_navigation_for_module,
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


def _generated_campaign_name_increase(self, name):
    """Продвинуть generated-этап только по явному ребру navigation-policy."""

    current = str(name or "").strip().upper()
    custom = self.config.STAGE_INCREASE_CUSTOM
    if custom:
        sequences = [custom] if isinstance(custom, str) else custom
        for sequence in sequences:
            stages = [item.strip().upper() for item in str(sequence).split('>')]
            if current not in stages:
                continue
            index = stages.index(current) + 1
            if index >= len(stages):
                logger.info('Достигнут конец пользовательской последовательности этапов')
                return current
            target = stages[index]
            if self._campaign_stage_exists(target):
                return target
            logger.info(
                f'Пользовательская последовательность указывает на недоступный этап {target}'
            )
            return current

    navigation = getattr(type(self), '_generated_event_stage_navigation', None)
    target = str(getattr(navigation, 'auto_next', '') or '').strip()
    if not target:
        logger.info('Для generated-этапа не задан следующий автоматический переход')
        return current
    if self._campaign_stage_exists(target):
        target = target.upper()
        logger.info(
            f'Следующий generated-этап по navigation-policy: {current} -> {target}'
        )
        return target
    logger.info(
        f'Navigation-policy указывает на недоступный generated-этап {target}; '
        'переход остановлен'
    )
    return current


def _generated_campaign_set_chapter(self, name, mode="normal"):
    """Перейти к generated-этапу по явному UI-маршруту navigation-policy.

    Для generated-карты её ``difficulty`` является частью проверенного маршрута
    и имеет приоритет над legacy-аргументом ``mode``. Аргумент используется как
    fallback только если navigation-policy не задаёт сложность явно.
    """

    navigation = getattr(type(self), '_generated_event_stage_navigation', None)
    if navigation is None or not navigation.has_ui_route:
        logger.warning('[Кампания — UI] Для generated-этапа отсутствует проверенный UI-маршрут')
        raise CampaignNameError

    effective_difficulty = navigation.difficulty or str(mode or "").strip()
    if effective_difficulty:
        self.config.override(Campaign_Mode=effective_difficulty)

    if navigation.ui_page == "event":
        self.ui_goto_event()
    elif navigation.ui_page == "sp":
        self.ui_goto_sp()
    elif navigation.ui_page == "campaign":
        self.ui_goto_campaign()
    else:
        logger.warning(
            f'[Кампания — UI] Неизвестная страница generated-этапа: '
            f'{navigation.ui_page!r}'
        )
        raise CampaignNameError

    if self.config.MAP_CHAPTER_SWITCH_20260326:
        if navigation.ui_mode:
            self.campaign_ensure_mode_20241219(navigation.ui_mode)
        if navigation.ui_aside:
            self.campaign_ensure_aside_20260326(navigation.ui_aside)
    elif self.config.MAP_CHAPTER_SWITCH_20241219:
        if navigation.ui_mode:
            self.campaign_ensure_mode_20241219(navigation.ui_mode)
        if navigation.ui_aside:
            self.campaign_ensure_aside_20241219(navigation.ui_aside)
    else:
        if navigation.ui_aside:
            logger.warning(
                '[Кампания — UI] Navigation-policy требует боковую вкладку, '
                'но активная раскладка её не поддерживает'
            )
            raise CampaignNameError
        if navigation.ui_mode:
            self.campaign_ensure_mode(navigation.ui_mode)

    if navigation.ui_chapter_index:
        self.campaign_ensure_chapter(navigation.ui_chapter_index)


def _generated_campaign_get_entrance(self, name):
    """Найти вход generated-этапа по явным именам navigation-policy."""

    navigation = getattr(type(self), '_generated_event_stage_navigation', None)
    if navigation is None or not navigation.entrance_names:
        logger.warning('[Кампания — UI] Для generated-этапа не заданы имена входа')
        raise CampaignNameError

    entrance_name = str(name)
    available = {
        str(stage_name).casefold(): stage_name
        for stage_name in self.stage_entrance
    }
    for candidate in navigation.entrance_names:
        key = available.get(str(candidate).casefold())
        if key is None:
            continue
        entrance = self.stage_entrance[key]
        entrance.name = entrance_name
        return entrance

    logger.warning(
        f'[Кампания — UI] Вход generated-этапа не найден: '
        f'{", ".join(navigation.entrance_names)}'
    )
    raise CampaignNameError


def _adapt_generated_campaign_ui(module: ModuleType, ui_layout: str | None = None) -> None:
    """Настроить канонический ``MAP`` под проверенные policy generated-события.

    Сам класс ``Campaign`` не копируется: адаптируется тот же объект класса из
    канонического сгенерированного модуля, причём не более одного раза.
    """

    _apply_generated_campaign_ui_policy(module, ui_layout)
    campaign_class = getattr(module, "Campaign", None)
    map_object = getattr(module, "MAP", None)
    if campaign_class is None or map_object is None:
        return

    module_name = str(getattr(module, "__name__", "") or "")
    is_generated_module = module_name.startswith("campaign.generated_event.")
    navigation = (
        generated_stage_navigation_for_module(module_name)
        if is_generated_module
        else None
    )
    if is_generated_module:
        campaign_class._generated_event_stage_navigation = navigation
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

    if is_generated_module:
        # Для generated-карт отсутствие navigation-policy означает безопасную остановку
        # автопродвижения вместо возврата к статическим legacy-последовательностям.
        campaign_class.campaign_name_increase = _generated_campaign_name_increase
        if navigation is not None and navigation.has_ui_route:
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
