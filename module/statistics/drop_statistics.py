"""Модуль пакетного разбора и статистики дропа по скриншотам (Drop Statistics).

Предоставляет функционал извлечения информации о выпавших предметах из пачки боевых скриншотов.
Поддерживает двухэтапный рабочий процесс:
    1. Извлечение шаблонов предметов из скриншотов (template extraction).
    2. Разбор данных дропа с помощью шаблонов и OCR с экспортом в CSV-файл.

Типичное использование:
    1. Настроить DROP_FOLDER (папка со скриншотами) и TEMPLATE_FOLDER (папка с шаблонами).
    2. Вызвать extract_template() для извлечения шаблонов, переименовать нужные вручную и вызвать extract_drop() для экспорта.

Используется для офлайн-анализа статистики большого количества скриншотов боев без подключения к реальному устройству.
"""

import csv
import shutil

from tqdm import tqdm

from module.base.decorator import cached_property
from module.base.utils import load_image
from module.logger import logger
from module.ocr.al_ocr import AlOcr
from module.ocr.ocr import Ocr
from module.statistics.battle_status import BattleStatusStatistics
from module.statistics.campaign_bonus import CampaignBonusStatistics
from module.statistics.get_items import GetItemsStatistics
from module.statistics.utils import *


class DropStatistics:
    """Обработчик пакетного разбора и статистики дропа по скриншотам.

    Загружает боевые скриншоты из указанной папки, распознает выпавшие предметы
    сопоставлением шаблонов и OCR, выводя результат в CSV-файл.
    Поддерживает режимы извлечения шаблонов и экспорта данных.

    Атрибуты класса:
        DROP_FOLDER (str): Корневой каталог скриншотов, по умолчанию './screenshots'.
        TEMPLATE_FOLDER (str): Имя папки шаблонов относительно DROP_FOLDER.
        TEMPLATE_BASIC (str): Каталог базовых шаблонов ресурсов.
        CNOCR_CONTEXT (str): Устройство инференса OCR, 'cpu' или 'gpu'.
        CSV_FILE (str): Имя выходного CSV-файла.
        CSV_OVERWRITE (bool): Перезаписывать ли существующий CSV перед экспортом.
        CSV_ENCODING (str): Кодировка CSV-файла, по умолчанию 'utf-8'.

    Examples:
        >>> stat = DropStatistics()
        >>> stat.extract_template('campaign_13_1')   # Шаг 1: извлечение шаблонов
        >>> stat.extract_drop('campaign_13_1')        # Шаг 3: экспорт данных дропа
    """

    DROP_FOLDER = './screenshots'
    TEMPLATE_FOLDER = 'item_templates'
    TEMPLATE_BASIC = './assets/stats_basic'
    CNOCR_CONTEXT = 'cpu'
    CSV_FILE = 'drop_result.csv'
    CSV_OVERWRITE = True
    CSV_ENCODING = 'utf-8'

    def __init__(self):
        AlOcr.CNOCR_CONTEXT = DropStatistics.CNOCR_CONTEXT
        Ocr.SHOW_LOG = False
        if not os.path.exists(self.template_folder):
            shutil.copytree(DropStatistics.TEMPLATE_BASIC, self.template_folder)

        self.battle_status = BattleStatusStatistics()
        self.get_items = GetItemsStatistics()
        self.campaign_bonus = CampaignBonusStatistics()
        self.get_items.load_template_folder(self.template_folder)

    @property
    def template_folder(self):
        return os.path.join(DropStatistics.DROP_FOLDER, DropStatistics.TEMPLATE_FOLDER)

    @property
    def csv_file(self):
        return os.path.join(DropStatistics.DROP_FOLDER, DropStatistics.CSV_FILE)

    @staticmethod
    def drop_folder(campaign):
        return os.path.join(DropStatistics.DROP_FOLDER, campaign)

    @cached_property
    def csv_overwrite_check(self):
        """Удаляет существующий CSV-файл, вызывается только один раз."""
        if DropStatistics.CSV_OVERWRITE:
            if os.path.exists(self.csv_file):
                logger.info(f'Удаление существующего CSV-файла: {self.csv_file}')
                os.remove(self.csv_file)
        return True

    def parse_template(self, file):
        """Извлекает шаблоны из одного файла, новым шаблонам присваивается автоинкрементный ID."""
        images = unpack(load_image(file))
        for image in images:
            if self.get_items.appear_on(image):
                self.get_items.extract_template(image, folder=self.template_folder)
            if self.campaign_bonus.appear_on(image):
                self.campaign_bonus.extract_template(image, folder=self.template_folder)

    def parse_drop(self, file):
        """Разбирает отдельный файл скриншота, извлекая данные о дропе.

        Args:
            file (str): Путь к файлу скриншота.

        Yields:
            list: Строка вида [метка_времени, кампания, имя_врага, тип_дропа, название_предмета, количество].
        """
        ts = os.path.splitext(os.path.basename(file))[0]
        campaign = os.path.basename(os.path.abspath(os.path.join(file, '../')))
        images = unpack(load_image(file))
        enemy_name = 'unknown'
        for image in images:
            if self.battle_status.appear_on(image):
                enemy_name = self.battle_status.stats_battle_status(image)
            if self.get_items.appear_on(image):
                for item in self.get_items.stats_get_items(image):
                    yield [ts, campaign, enemy_name, 'GET_ITEMS', item.name, item.amount]
            if self.campaign_bonus.appear_on(image):
                for item in self.campaign_bonus.stats_get_items(image):
                    yield [ts, campaign, enemy_name, 'CAMPAIGN_BONUS', item.name, item.amount]

    def extract_template(self, campaign):
        """Извлекает изображения шаблонов из папки указанной кампании.

        Args:
            campaign (str): Название кампании.
        """
        print('')
        logger.hr(f'Извлечение шаблонов из {campaign}', level=1)
        for ts, file in tqdm(load_folder(self.drop_folder(campaign)).items()):
            try:
                self.parse_template(file)
            except ImageError as e:
                logger.warning(e)
                continue
            except Exception as e:
                logger.exception(e)
                logger.warning(f'Ошибка изображения {ts}')
                continue

    def extract_drop(self, campaign):
        """Разбирает данные дропа из папки указанной кампании и записывает в CSV.

        Args:
            campaign (str): Название кампании.
        """
        print('')
        logger.hr(f'Извлечение данных о наградах из {campaign}', level=1)
        _ = self.csv_overwrite_check

        with open(self.csv_file, 'a', newline='', encoding=DropStatistics.CSV_ENCODING) as csv_file:
            writer = csv.writer(csv_file)
            for ts, file in tqdm(load_folder(self.drop_folder(campaign)).items()):
                try:
                    rows = list(self.parse_drop(file))
                    writer.writerows(rows)
                except ImageError as e:
                    logger.warning(e)
                    continue
                except Exception as e:
                    logger.exception(e)
                    logger.warning(f'Ошибка изображения {ts}')
                    continue


if __name__ == '__main__':
    # Каталог со скриншотами наград; по умолчанию './screenshots'
    DropStatistics.DROP_FOLDER = './screenshots'
    # Каталог шаблонов для загрузки и сохранения шаблонов
    # Путь: {DROP_FOLDER}/{TEMPLATE_FOLDER}
    # Если каталог отсутствует, он автоматически копируется из './assets/stats_basic'
    DropStatistics.TEMPLATE_FOLDER = 'campaign_13_1_template'
    # 'cpu' или 'gpu'; по умолчанию 'cpu'
    # 'gpu' ускоряет распознавание, но требует GPU-версии mxnet
    DropStatistics.CNOCR_CONTEXT = 'cpu'
    # Имя выходного CSV-файла
    # Путь: {DROP_FOLDER}/{CSV_FILE}
    DropStatistics.CSV_FILE = 'drop_results.csv'
    # При True существующий файл удаляется перед извлечением
    DropStatistics.CSV_OVERWRITE = True
    # Обычно 'utf-8'
    # Используйте 'gbk', если китайский текст отображается в Excel некорректно
    DropStatistics.CSV_ENCODING = 'gbk'
    # Список кампаний в DROP_FOLDER, данные которых нужно экспортировать
    # Путь: {DROP_FOLDER}/{CAMPAIGN}
    # Ниже только пример; измените в соответствии с реальной конфигурацией
    CAMPAIGNS = ['campaign_13_1']

    stat = DropStatistics()

    """
    Шаг 1:
        Раскомментируйте следующий код и запустите, после выполнения снова закомментируйте.
    """
    # for i in CAMPAIGNS:
    #     stat.extract_template(i)

    """
    Шаг 2:
        Перейдите в {DROP_FOLDER}/{TEMPLATE_FOLDER}
        и вручную переименуйте интересующие вас файлы шаблонов.
    """
    pass

    """
    Шаг 3:
        Раскомментируйте следующий код и запустите, после выполнения снова закомментируйте.
        Результаты сохраняются в {DROP_FOLDER}/{CSV_FILE}.
    """
    for i in CAMPAIGNS:
        stat.extract_drop(i)
