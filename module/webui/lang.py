"""Единственный runtime-загрузчик русской локализации WebUI."""

from typing import Dict

from module.config.deep import deep_iter
from module.config.locale import UI_LOCALE
from module.config.utils import filepath_i18n, read_file
from module.submodule.utils import list_mod_dir

LANG = UI_LOCALE
TRANSLATE_MODE = False

# Точечные переопределения относятся только к Data Logger. Глобальная замена
# названий во всех строках намеренно не используется: она могла затронуть
# несвязанные подсказки и скрывала устаревшие исходные переводы.
_OPSI_DATA_LOGGER_TRANSLATIONS = {
    "OpsiExplore._info.help": (
        "Исследует все морские зоны Операции «Сирена» в начале месяца.\n"
        "Если Operation Siren Data Logger подтверждён для текущего месячного "
        "цикла, бот использует активированный эффект; иначе выдерживает "
        "27-минутное восстановление поиска воздушного пространства.\n"
        "Требуется завершить основной сюжет, `Simulation Battle` и "
        "`Siren Proving Ground`.\n"
        "После выполнения условий задача запускается автоматически."
    ),
    "OpsiExplore.SpecialRadar.name": (
        "Покупать и активировать Operation Siren Data Logger после сброса"
    ),
    "OpsiExplore.SpecialRadar.help": (
        "После ежемесячного сброса бот проверяет предмет в магазине ваучеров. "
        "Если предмет доступен — покупает его, затем переходит в союзный порт "
        "и активирует. Успешный результат запоминается до следующего сброса. "
        "Другие координатные логгеры не используются."
    ),
}

dic_lang: Dict[str, str] = {}


def set_language(value: str, refresh: bool = False) -> None:
    """Совместимый shim: разрешён только обязательный ``ru-RU``.

    Функция больше не записывает deploy-конфигурацию и не перезагружает страницу.
    """
    if not isinstance(value, str) or value.lower() != UI_LOCALE.lower():
        raise ValueError(f"Поддерживается только язык интерфейса {UI_LOCALE}.")
    if refresh:
        raise ValueError("Перезагрузка для смены языка больше не поддерживается.")


def t(key, *args, **kwargs):
    """Вернуть русскую строку и применить исходные format-аргументы."""
    if TRANSLATE_MODE:
        return key
    return _t(key).format(*args, **kwargs)


def _t(key, lang=None):
    """Вернуть перевод без foreign-locale fallback."""
    if lang is not None and str(lang).lower() != UI_LOCALE.lower():
        raise ValueError(f"Поддерживается только язык интерфейса {UI_LOCALE}.")
    try:
        return dic_lang[key]
    except KeyError:
        print(f"Отсутствует обязательный ключ русской локализации: {key}")
        return key


def reload() -> None:
    """Перезагрузить только ``ru-RU`` из основного каталога и модулей."""
    loaded: Dict[str, str] = {}
    for mod_name, _ in list_mod_dir():
        module_file = filepath_i18n(UI_LOCALE, mod_name)
        for path, value in deep_iter(read_file(module_file), depth=3):
            loaded[".".join(path)] = value

    for path, value in deep_iter(read_file(filepath_i18n(UI_LOCALE)), depth=3):
        loaded[".".join(path)] = value

    for key, value in _OPSI_DATA_LOGGER_TRANSLATIONS.items():
        if key in loaded:
            loaded[key] = value

    dic_lang.clear()
    dic_lang.update(loaded)
