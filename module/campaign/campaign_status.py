"""Модуль отслеживания состояния кампании.

Считывает числовую информацию на экране кампании через OCR:
- Количество припасов (монет)
- Количество нефти
- Очки события (PT)
- Проверка лимитов нефти и припасов

Эти данные используются для проверки условий остановки (например, исчерпание нефти, переполнение монет и т. д.).

Класс PtOcr отвечает за распознавание очков события,
требуя специальной предварительной обработки изображения (инверсия, удаление фона и т. д.).

Наследуется от UI, используя механизмы навигации по экранам.
"""

import datetime
import re

import cv2
import numpy as np

import module.config.server as server

from module.base.timer import Timer
from module.campaign.assets import OCR_EVENT_PT, OCR_COIN, OCR_OIL, OCR_COIN_LIMIT, OCR_OIL_LIMIT, OCR_OIL_CHECK
from module.base.utils import color_similar, get_color
from module.logger import logger
from module.ocr.ocr import Digit, Ocr
from module.ui.ui import UI
from module.log_res import LogRes

#if server.server != 'jp':
#    OCR_COIN = Digit(OCR_COIN, name='OCR_COIN', letter=(239, 239, 239), threshold=128)
#else:
#    OCR_COIN = Digit(OCR_COIN, name='OCR_COIN', letter=(201, 201, 201), threshold=128)

class PtOcr(Ocr):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, lang='azur_lane', alphabet='X0123456789', **kwargs)

    def pre_process(self, image):
        """
        Предварительно обрабатывает изображение с цифрами очков события (PT).

        Args:
            image (np.ndarray): Изображение формата (height, width, channel).

        Returns:
            np.ndarray: Оттенки серого формата (width, height).
        """
        # Берём максимальное значение из трёх каналов RGB
        r, g, b = cv2.split(cv2.subtract((255, 255, 255), image))
        image = cv2.min(cv2.min(r, g), b)
        # Удаляем фон, отображая диапазон 0–192 в 0–255
        image = cv2.multiply(image, 255 / 192)

        return image.astype(np.uint8)


OCR_PT = PtOcr(OCR_EVENT_PT)


class CampaignStatus(UI):
    def get_event_pt(self, update=False):
        """
        Получает количество очков события (PT).

        Returns:
            int: Количество PT, либо 0 при сбое распознавания.
        """
        pt = OCR_PT.ocr(self.device.image)

        # В первую очередь ищем формат с префиксом X: исторически некоторые события использовали «X1234»
        res = re.search(r'X(\d+)', pt)
        if res:
            pt = int(res.group(1))
            logger.attr('Очки события', pt)
            LogRes(self.config).Pt = pt
        else:
            # Резервный вариант: принимаем и чисто числовой результат OCR, сохраняя предупреждение для диагностики
            res2 = re.search(r'(\d+)', pt)
            if res2:
                num = int(res2.group(1))
                logger.warning(f"Недопустимый формат результата PT (нет 'X'): {pt}; использую только цифры: {num}")
                logger.attr('Очки события — резервное значение', num)
                LogRes(self.config).Pt = num
                pt = num
            else:
                logger.warning(f'Недопустимый результат PT: {pt}')
                pt = 0
        if update:
            self.config.update()
        return pt

    def get_coin(self, skip_first_screenshot=True, update=False):
        """
        Получает количество монет (припасов).

        Returns:
            int: Количество монет.
        """
        _coin = {}
        timeout = Timer(1, count=2).start()
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if timeout.reached():
                logger.warning('Истекло время получения количества монет')
                break

            _coin = {
                'Value': self._get_num(OCR_COIN, 'OCR_COIN', (239, 239, 239)),
                'Limit': self._get_num(OCR_COIN_LIMIT, 'OCR_COIN_LIMIT', (239, 239, 239))
            }
            if _coin['Value'] >= 100:
                break
        LogRes(self.config).Coin = _coin
        if update:
            self.config.update()

        return _coin['Value']

    def _get_num(self, _button, name, letter=(247, 247, 247)):
        # Обновляем смещение
        _ = self.appear(OCR_OIL_CHECK)

        color = get_color(self.device.image, OCR_OIL_CHECK.button)
        if color_similar(color, OCR_OIL_CHECK.color):
            # Исходный цвет
            if isinstance(_button, Ocr):
                ocr = _button
            else:
                if server.server != 'jp':
                    ocr = Digit(_button, name=name, letter=letter, threshold=128)
                else:
                    ocr = Digit(_button, name=name, letter=(201, 201, 201), threshold=128)
        elif color_similar(color, (59, 59, 64)):
            # С чёрной маской
            ocr = Digit(_button, name=name, letter=(165, 165, 165), threshold=128)
        else:
            logger.warning('[Кампания — состояние] Неожиданный цвет OCR_OIL_CHECK')
            ocr = Digit(_button, name=name, letter=(247, 247, 247), threshold=128)

        return ocr.ocr(self.device.image)

    def get_oil_snapshot(self, skip_first_screenshot=True, update=False, record=True):
        """Считать значение и отображаемый предел нефти с текущего экрана."""
        _oil = {}
        timeout = Timer(1, count=2).start()
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if not self.appear(OCR_OIL_CHECK, offset=(10, 2)):
                logger.info('Значок нефти отсутствует')
                self.device.sleep(1)

            if timeout.reached():
                logger.warning('Истекло время получения количества нефти')
                break

            _oil = {
                'Value': self._get_num(OCR_OIL, 'OCR_OIL', (247, 247, 247)),
                'Limit': self._get_num(OCR_OIL_LIMIT, 'OCR_OIL_LIMIT', (247, 247, 247))
            }
            if _oil['Value'] >= 100:
                break
        if record:
            LogRes(self.config).Oil = _oil
        if update and record:
            self.config.update()
        return _oil

    def get_oil(self, skip_first_screenshot=True, update=False):
        """
        Получает количество нефти.

        Returns:
            int: Количество нефти.
        """
        return self.get_oil_snapshot(
            skip_first_screenshot=skip_first_screenshot,
            update=update,
        ).get('Value', 0)

    def is_balancer_task(self):
        """
        Определяет, является ли текущая задача задачей события (исключая ежедневные задачи события).

        Returns:
            bool: Является ли задачей события.
        """
        tasks = [
            'Event',
            'Event2',
            'Raid',
            'Coalition',
            'GemsFarming',
            'ThreeOilLowCost',
        ]
        command = self.config.Scheduler_Command
        if command in tasks:
            if self.config.Campaign_Event == 'campaign_main':
                return False
            else:
                return True
        else:
            return False
