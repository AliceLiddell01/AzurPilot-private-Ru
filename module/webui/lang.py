"""Единственный runtime-загрузчик русской локализации WebUI."""

from typing import Dict

from module.config.deep import deep_iter
from module.config.locale import UI_LOCALE
from module.config.utils import filepath_i18n, read_file
from module.submodule.utils import list_mod_dir

LANG = UI_LOCALE
TRANSLATE_MODE = False

# Точечные runtime-переопределения используются только для персональных
# контрактов, которые должны переживать повторную генерацию upstream i18n.
# Глобальная замена названий во всех строках намеренно не используется: она
# могла бы затронуть несвязанные подсказки и скрыть устаревшие переводы.
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

_RECOVERY_STAGE3_TRANSLATIONS = {
    "Error.GameStuckRestart.name": (
        "Восстанавливать эмулятор после неудачного перезапуска Azur Lane"
    ),
    "Error.GameStuckRestart.help": (
        "Включено по умолчанию. При зависании AzurPilot сначала перезапускает "
        "только Azur Lane и проверяет восстановление. Если игра не восстановилась, "
        "бот пытается штатно перезапустить эмулятор. Для выбранного экземпляра "
        "MuMu принудительное завершение допускается только после неудачной "
        "штатной остановки и проверки, что экземпляр всё ещё запущен. Настройку "
        "можно отключить вручную."
    ),
    "Error.GameStuckThreshold.name": (
        "Предел последовательных recovery-инцидентов после зависания"
    ),
    "Error.GameStuckThreshold.help": (
        "Максимальное число последовательных инцидентов GameStuckError или "
        "GameTooManyClickError, для которых разрешена цепочка восстановления. "
        "Внутренние попытки запуска эмулятора не увеличивают этот счётчик. "
        "После следующей обычной успешно завершённой задачи счётчик сбрасывается."
    ),
    "Error.AdbOfflineRestart.name": (
        "Восстанавливать эмулятор при недоступности ADB"
    ),
    "Error.AdbOfflineRestart.help": (
        "Включено по умолчанию. Если устройство или ADB недоступны, AzurPilot "
        "запускает ограниченную проверяемую цепочку восстановления эмулятора. "
        "Для MuMu сначала используется штатная остановка; instance-scoped hard "
        "kill возможен только если выбранный экземпляр не остановился. Настройку "
        "можно отключить вручную."
    ),
    "Error.AdbOfflineThreshold.name": (
        "Предел последовательных recovery-инцидентов ADB"
    ),
    "Error.AdbOfflineThreshold.help": (
        "Максимальное число последовательных ADB/transport-инцидентов, для "
        "которых разрешено автоматическое восстановление эмулятора. Это отдельный "
        "budget от зависаний игры; после следующей обычной успешно завершённой "
        "задачи счётчик сбрасывается."
    ),
}

# Эти ключи принадлежат персональному WebUI и не должны попадать в большие
# генерируемые каталоги локализации. Runtime интерфейса поддерживает только ru-RU.
_EVENT_DASHBOARD_TRANSLATIONS = {
    "Gui.Dashboard.EventPtTotal": "Всего валюты события заработано",
    "Gui.Dashboard.EventCurrencyBalance": "Текущий баланс валюты события",
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

    for overrides in (
        _OPSI_DATA_LOGGER_TRANSLATIONS,
        _RECOVERY_STAGE3_TRANSLATIONS,
    ):
        for key, value in overrides.items():
            if key in loaded:
                loaded[key] = value

    loaded.update(_EVENT_DASHBOARD_TRANSLATIONS)

    dic_lang.clear()
    dic_lang.update(loaded)
