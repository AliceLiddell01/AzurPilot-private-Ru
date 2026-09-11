"""Заглушка PIL для облегчённого запуска WebUI.

В процессах, которым не нужна обработка изображений, можно временно
подменить PIL в ``sys.modules``. Подмена не должна вытеснять уже загруженный
настоящий PIL: иначе его внутренний реестр обработчиков изображений теряется.
"""

import sys
from types import ModuleType


def _is_fake_pil_image_module(module: object) -> bool:
    """Проверить, что модуль является нашей заглушкой ``PIL.Image``."""

    return (
        isinstance(module, ModuleType)
        and getattr(module, "__name__", "") == "PIL.Image"
        and getattr(module, "__file__", None) is None
    )


def _is_fake_pil_module(module: object) -> bool:
    """Проверить, что модуль является нашей заглушкой ``PIL``."""

    return (
        isinstance(module, ModuleType)
        and getattr(module, "__name__", "") == "PIL"
        and not hasattr(module, "__path__")
        and _is_fake_pil_image_module(getattr(module, "Image", None))
    )


def import_fake_pil_module() -> None:
    """Установить заглушку, только если настоящий PIL ещё не загружен."""

    current_pil = sys.modules.get("PIL")
    current_image = sys.modules.get("PIL.Image")
    if current_pil is not None and not _is_fake_pil_module(current_pil):
        return
    if current_image is not None and not _is_fake_pil_image_module(current_image):
        return
    if _is_fake_pil_module(current_pil):
        return

    fake_pil_module = ModuleType('PIL')
    fake_pil_module.Image = ModuleType('PIL.Image')
    fake_pil_module.Image.Image = type('MockPILImage', (), dict(__init__=None))
    sys.modules['PIL'] = fake_pil_module
    sys.modules['PIL.Image'] = fake_pil_module.Image


def remove_fake_pil_module() -> None:
    """Удалить только нашу заглушку и сохранить настоящий PIL нетронутым."""

    current_pil = sys.modules.get("PIL")
    current_image = sys.modules.get("PIL.Image")
    if _is_fake_pil_module(current_pil):
        sys.modules.pop("PIL", None)
    if _is_fake_pil_image_module(current_image):
        sys.modules.pop("PIL.Image", None)
