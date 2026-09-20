"""
Распознавание номера серии исследований.

Модуль распознает номера серий исследовательских проектов (S1-S9) по скриншотам с помощью сопоставления шаблонов.

Номера серий отображаются римскими цифрами на карточках проектов. Из-за эффекта перспективы
карточки на разных позициях имеют масштабные различия, поэтому RESEARCH_SCALING
используется для компенсации масштабирования шаблонов для каждой позиции.

Основные возможности:
- Распознавание серий в списке: вырезание областей индикаторов серий на карточках проектов
  и поочередное сопоставление оттенков серого по шаблонам для 5 проектов
- Распознавание серии на странице деталей: определение номера серии на странице подробностей проекта

Терминология:
    Серия (Series): номер серии исследований S1-S9, соответствующий определенному пулу кораблей исследований
"""
from module.base.utils import area_pad, crop, rgb2gray
from module.research.assets import *

RESEARCH_SERIES = (SERIES_1, SERIES_2, SERIES_3, SERIES_4, SERIES_5)
RESEARCH_SCALING = [
    424 / 558,
    491 / 558,
    1.0,
    491 / 558,
    424 / 558,
]


def match_series(image, scaling):
    """
    Распознает номер серии отдельного исследовательского проекта по шаблону.

    Сопоставление шаблонов выполняется в порядке от S9 к S1 с приоритетом высших номеров серий
    для предотвращения ложных срабатываний шаблонов младших номеров.

    Args:
        image (np.ndarray): Вырезанное полутоновое изображение области индикатора серии.
        scaling (float): Масштабный коэффициент сопоставления шаблона для компенсации перспективы.

    Returns:
        int: Номер серии (1-9), либо 0 при неудачном сопоставлении.
    """
    image = rgb2gray(image)

    if TEMPLATE_S9.match(image, scaling=scaling):
        return 9
    if TEMPLATE_S8.match(image, scaling=scaling):
        return 8
    if TEMPLATE_S7.match(image, scaling=scaling):
        return 7
    if TEMPLATE_S6.match(image, scaling=scaling):
        return 6
    if TEMPLATE_S4_2.match(image, scaling=scaling):
        return 4
    if TEMPLATE_S4.match(image, scaling=scaling):
        return 4
    if TEMPLATE_S5.match(image, scaling=scaling):
        return 5
    if TEMPLATE_S3.match(image, scaling=scaling):
        return 3
    if TEMPLATE_S2.match(image, scaling=scaling):
        return 2
    if TEMPLATE_S1.match(image, scaling=scaling):
        return 1
    return 0


def get_research_series_3(image, series_button=RESEARCH_SERIES):
    """
    Пакетно распознает номера серий для 5 проектов по скриншоту списка исследований.

    Вырезает области индикаторов серий на карточках проектов и поочередно распознает
    номера серий сопоставлением шаблонов с компенсацией масштабирования. Проекты
    на разных позициях требуют различных коэффициентов масштабирования из-за перспективы (задаются RESEARCH_SCALING).

    Args:
        image (np.ndarray): Полный скриншот страницы списка исследований.
        series_button (list[Button]): Определения кнопок для 5 областей индикаторов серий.

    Returns:
        list[int]: Список номеров серий 5 проектов, например [1, 3, 5, 4, 2].
    """
    return [
        match_series(crop(image, area_pad(button.area, pad=-10), copy=False), scaling)
        for scaling, button in zip(RESEARCH_SCALING, series_button)
    ]


def get_detail_series(image):
    """
    Распознает номер серии по скриншоту страницы подробностей проекта.

    Вырезает область индикатора серии на странице деталей и распознает серию
    исследуемого проекта сопоставлением шаблона (с коэффициентом масштабирования 1.0).

    Args:
        image (np.ndarray): Скриншот страницы деталей исследования.

    Returns:
        int: Номер серии (1-9), либо 0 при неудачном сопоставлении.
    """
    return match_series(crop(image, area_pad(SERIES_DETAIL.area, pad=-30), copy=False), scaling=1.0)
