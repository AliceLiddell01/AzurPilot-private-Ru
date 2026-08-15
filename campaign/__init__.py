"""Ленивый compatibility-мост для текущих generated Event-карт.

Обычный `import campaign` не читает Event registry и не исполняет карты.
Разрешение выполняется только при попытке импортировать конкретный этап из
legacy selector текущего события.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import sys
from functools import wraps
from types import ModuleType

from module.config.time_source import now as current_time
from module.event_datamine.campaign_selector import resolve_generated_campaign_module


def _adapt_generated_campaign_ui(module: ModuleType) -> None:
    """Навигировать по каноническому имени MAP при legacy T/HT alias.

    Сам Campaign-класс не копируется: адаптируется тот же объект класса из
    канонического generated-модуля, причём не более одного раза.
    """

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

    campaign_class.ensure_campaign_ui = ensure_campaign_ui
    campaign_class._generated_event_ui_adapted = True


class _GeneratedEventAliasLoader(importlib.abc.Loader):
    """Вернуть существующий канонический generated-модуль без повторного исполнения."""

    def __init__(self, target: str):
        self.target = target

    def create_module(self, spec):
        module = importlib.import_module(self.target)
        _adapt_generated_campaign_ui(module)
        return module

    def exec_module(self, module: ModuleType) -> None:
        # Канонический модуль уже исполнен importlib.import_module() ровно один раз.
        return None


class _GeneratedEventAliasFinder(importlib.abc.MetaPathFinder):
    """Лениво сопоставить legacy Event stage с generated current stage."""

    def __init__(self, now_factory=current_time):
        self._now_factory = now_factory

    def find_spec(self, fullname, path=None, target=None):
        parts = str(fullname).split(".")
        if len(parts) != 3 or parts[0] != "campaign":
            return None
        selector, stage = parts[1], parts[2]
        if not selector.startswith("event_") or selector == "event_generated":
            return None
        resolved = resolve_generated_campaign_module(
            selector,
            stage,
            now=self._now_factory(),
        )
        if resolved is None:
            return None
        return importlib.util.spec_from_loader(
            fullname,
            _GeneratedEventAliasLoader(resolved),
        )


def _install_generated_event_alias_finder() -> None:
    """Зарегистрировать finder один раз без чтения registry на старте."""

    if any(isinstance(item, _GeneratedEventAliasFinder) for item in sys.meta_path):
        return
    sys.meta_path.insert(0, _GeneratedEventAliasFinder())


_install_generated_event_alias_finder()
