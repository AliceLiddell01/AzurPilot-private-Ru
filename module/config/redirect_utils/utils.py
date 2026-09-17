"""Набор утилит перенаправления конфигурации.

Предоставляет функции преобразования значений при обновлении версий конфигурации.
При изменении схемы конфигурации (переименование опций, смена формата значений)
эти функции переводят устаревшие форматы настроек в актуальные.

Функции перенаправления вызываются в ConfigUpdater.config_redirect(),
благодаря чему пользователю не требуется вручную редактировать файлы конфигурации после обновления.

Типичные сценарии перенаправления:
- Изменение наименований вариантов (например, 'auto' → 'default')
- Изменение формата значений (например, логическое значение → элемент перечисления)
- Нормализация названий серверов
"""


def upload_redirect(value):
    """
    redirect attr about upload.
    """
    if isinstance(value, list):
        if not value[0] and not value[1]:
            return 'do_not'
        elif value[0] and not value[1]:
            return 'save'
        elif not value[0] and value[1]:
            return 'upload'
        else:
            return 'save_and_upload'
    else:
        if not value:
            return 'do_not'
        else:
            return 'save'


def dossier_redirect(value):
    """
    OpsiDossierBeacon -> AttackMode
    """
    if value:
        return 'current_dossier'
    else:
        return 'current'


def enhance_favourite_redirect(value):
    """
    EnhanceFavourite -> ShipToEnhance
    """
    if value:
        return 'all'
    else:
        return 'favourite'


def enhance_check_redirect(value):
    """
    CheckPerCategory should be at least 5
    """
    if isinstance(value, int):
        if value < 5:
            return 5
    return value


def emotion_mode_redirect(value):
    """
    CalculateEmotion + IgnoreLowEmotionWarn -> Emotion.Mode
    """
    calculate, ignore = value
    if calculate:
        if ignore:
            return 'calculate_ignore'
        else:
            return 'calculate'
    else:
        if ignore:
            return 'ignore'
        else:
            # Invalid, fallback to calculate
            return 'calculate'


def change_ship_redirect(value):
    """
    FlagshipChange + FlagshipEquipChange -> ChangeFlagship
    """
    ship, equip = value
    if not ship:
        return 'disabled'
    elif equip:
        return 'ship_equip'
    else:
        return 'ship'


def coalition_to_frostfall(value):
    """
    Преобразовать общее название сложности во внутренний номер этапа события Frostfall.
    """
    if value == 'easy':
        return 'tc1'
    elif value == 'normal':
        return 'tc2'
    elif value == 'hard':
        return 'tc3'
    else:
        return value


def coalition_to_little_academy(value):
    """
    Преобразовать номер этапа TC из старого коллаборационного события в общее название сложности.
    """
    normalized = str(value).lower().replace('-', '')
    if normalized == 'tc1':
        return 'easy'
    elif normalized == 'tc2':
        return 'normal'
    elif normalized == 'tc3':
        return 'hard'
    else:
        return value
