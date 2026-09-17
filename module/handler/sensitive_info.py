"""Модуль маскирования конфиденциальной информации.

Маскирует персональные данные пользователя перед сохранением снимков экрана и выводом логов для защиты конфиденциальности.

Маскирование изображений:
- Главный экран: маскирование имени командира, UID и другой личной информации
- Экран профиля игрока: маскирование ID игрока и информации о сервере

Маскирование текста:
- Пути к файлам в логах заменяются на фиктивные пути (C:\\fakepath\\AzurLaneAutoScript)
- Реальные пути в ADB заменяются

Использует класс Mask для наложения масок; изображения масок хранятся в каталоге assets/mask/.
"""

import re

from module.base.mask import Mask
from module.ui.assets import PLAYER_CHECK
from module.ui.page import MAIN_GOTO_CAMPAIGN_WHITE, MAIN_GOTO_FLEET

# Изображения шаблонов маскирования.
MASK_MAIN = Mask('./assets/mask/MASK_MAIN.png')
MASK_MAIN_WHITE = Mask('./assets/mask/MASK_MAIN_WHITE.png')
MASK_PLAYER = Mask('./assets/mask/MASK_PLAYER.png')


def handle_sensitive_image(image):
    """Накладывает маски на области с конфиденциальной информацией на снимке экрана.

    Проверяет, содержит ли текущий снимок экраны с персональными данными (главный экран, профиль игрока и т. д.),
    и при обнаружении применяет соответствующие шаблоны маскирования.

    Args:
        image (np.ndarray): Входной снимок экрана.

    Returns:
        np.ndarray: Снимок экрана после наложения масок.
    """
    if PLAYER_CHECK.match(image, offset=(30, 30)):
        image = MASK_PLAYER.apply(image)
    if MAIN_GOTO_FLEET.match(image, offset=(30, 30)):
        image = MASK_MAIN.apply(image)
    if MAIN_GOTO_CAMPAIGN_WHITE.match(image, offset=(30, 30)):
        image = MASK_MAIN_WHITE.apply(image)

    return image


def handle_sensitive_text(text):
    """Обезличить чувствительные сведения о путях в тексте журнала.

    Заменить реальные пути в журнале на фиктивные, чтобы не раскрывать структуру
    каталогов пользователя.

    Args:
        text (str): Исходный текст.

    Returns:
        str: Обезличенный текст.
    """
    text = re.sub('File \"(.*?)AzurLaneAutoScript', 'File \"C:\\\\fakepath\\\\AzurLaneAutoScript', text)
    text = re.sub(r'\[Adb_binary\] (.*?)AzurLaneAutoScript', '[Adb_binary] C:\\\\fakepath\\\\AzurLaneAutoScript', text)
    return text


def handle_sensitive_logs(logs):
    return [handle_sensitive_text(line) for line in logs]
