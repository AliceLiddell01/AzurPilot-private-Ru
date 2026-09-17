"""Модуль военных архивов.

Автоматически выполняет этапы военных архивов Azur Lane. Военные архивы — это повторный запуск прошлых событий,
требующий расхода Ключей данных (Data Key), суточный лимит которых составляет 60 штук.

Основные функции модуля:
- Распознавание оставшихся Ключей данных через OCR для контроля выходов в бой
- Управление ограничением ежедневного числа выходов (настраиваемый суточный лимит)
- Автоматическая остановка задачи при исчерпании Ключей данных или дневного лимита
- Списание суточного лимита в реальном времени после прохождения этапа с сохранением в конфигурацию

Внимание: в военных архивах отключена функция автопоиска для продолжения боя,
так как размытый фон меню автопоиска перекрывает область OCR Ключей данных.

Пути конфигурации: WarArchives.DailyRunCount (дневной лимит выходов),
                  StopCondition.OilLimit (ограничение по нефти)
"""

import re

from campaign.campaign_war_archives.campaign_base import CampaignBase
from module.campaign.run import CampaignRun
from module.config.utils import get_server_last_update
from module.logger import logger
from module.ocr.ocr import DigitCounter
from module.war_archives.assets import (OCR_DATA_KEY_CAMPAIGN,
                                        WAR_ARCHIVES_CAMPAIGN_CHECK)


class OcrDataKey(DigitCounter):
    """OCR счётчика Ключей данных военных архивов.

    Обрабатывает OCR-распознавание количества Ключей данных и исправляет распространённые ошибки распознавания.
    Формат Ключей данных — «текущее/60», OCR может ошибочно распознать «/60» как «60».
    """

    def after_process(self, result):
        """Постобработка OCR для исправления ошибок распознавания количества Ключей данных.

        Исправляет ошибочный формат «X60» от OCR на «X/60».
        Например, результат распознавания «1560» преобразуется в «15/60».

        Args:
            result: Исходная строка результата распознавания OCR.

        Returns:
            Скорректированная строка количества Ключей данных.
        """
        result = super().after_process(result)
        result = re.sub(r'(\d{1,2})60$', r'\1/60', result)
        return result


DATA_KEY_CAMPAIGN = OcrDataKey(OCR_DATA_KEY_CAMPAIGN, letter=(255, 247, 247), threshold=64)


class CampaignWarArchives(CampaignRun, CampaignBase):
    """Исполнитель кампании военных архивов.

    Наследуется от CampaignRun (запуск кампании) и CampaignBase (база кампаний военных архивов),
    дополняя стандартную логику кампании ограничениями военных архивов:
    - Управление расходом Ключей данных (принудительное включение USE_DATA_KEY)
    - Ограничение суточного числа выходов (автосброс при наступлении нового дня, адаптация к смене настроек)
    - OCR-проверка Ключей данных (распознавание оставшегося количества на экране кампании архивов)
    - Отключение продолжения боя через автопоиск (во избежание перекрытия области OCR)
    """
    def daily_run_limit_reset(self):
        """Обновление суточного лимита выходов в военных архивах.

        DailyRunCount — установленный пользователем суточный максимум; DailyRunCountRemain — сохранённый остаток на сегодня.
        Если время записи старше последнего обновления сервера, значит наступил новый день и требуется восстановить полный лимит.
        """
        limit = self.config.WarArchives_DailyRunCount
        if limit <= 0:
            if self.config.WarArchives_DailyRunCountLimit != 0:
                with self.config.multi_set():
                    self.config.WarArchives_DailyRunCountRemain = 0
                    self.config.WarArchives_DailyRunCountLimit = 0
            return

        last_update = get_server_last_update(self.config.Scheduler_ServerUpdate)
        record = self.config.WarArchives_DailyRunCountRecord
        remain = self.config.WarArchives_DailyRunCountRemain
        old_limit = self.config.WarArchives_DailyRunCountLimit
        if record < last_update or remain > limit:
            logger.info(f'[Архивы] Сброс дневного числа выходов: {remain} -> {limit}')
            with self.config.multi_set():
                self.config.WarArchives_DailyRunCountRemain = limit
                self.config.WarArchives_DailyRunCountRecord = last_update
                self.config.WarArchives_DailyRunCountLimit = limit
        elif old_limit != limit:
            remain = max(remain + limit - old_limit, 0)
            remain = min(remain, limit)
            logger.info(f'[Архивы] Обновление дневного числа выходов: {old_limit} -> {limit}, осталось: {remain}')
            with self.config.multi_set():
                self.config.WarArchives_DailyRunCountRemain = remain
                self.config.WarArchives_DailyRunCountLimit = limit

    def daily_run_limit_triggered(self):
        """Проверка, исчерпан ли суточный лимит выходов в военных архивах."""
        limit = self.config.WarArchives_DailyRunCount
        if limit <= 0:
            return False

        remain = self.config.WarArchives_DailyRunCountRemain
        logger.info(f'[Архивы] На сегодня осталось выходов: {remain} / {limit}')
        if remain > 0:
            return False

        logger.hr('Условие остановки: дневное число выходов')
        self.config.task_delay(server_update=True)
        return True

    def daily_run_limit_consume(self):
        """Списание и сохранение суточного лимита выходов военных архивов после прохождения этапа."""
        limit = self.config.WarArchives_DailyRunCount
        if limit <= 0:
            return

        remain = max(self.config.WarArchives_DailyRunCountRemain - 1, 0)
        logger.info(f'[Архивы] На сегодня осталось выходов: {remain} / {limit}')
        with self.config.multi_set():
            self.config.WarArchives_DailyRunCountRemain = remain
            self.config.WarArchives_DailyRunCountRecord = get_server_last_update(self.config.Scheduler_ServerUpdate)
            self.config.WarArchives_DailyRunCountLimit = limit

    def after_campaign_run(self):
        """Немедленное списание суточного лимита выходов после одного прохождения этапа архивов."""
        self.daily_run_limit_consume()

    def triggered_stop_condition(self, oil_check=True):
        """Проверка условий остановки военных архивов.

        На экране кампании архивов распознаёт оставшееся количество Ключей данных через OCR.
        При исчерпании Ключей данных откладывает задачу до следующего сброса сервера.

        Pages:
            in: WAR_ARCHIVES_CAMPAIGN_CHECK (экран кампании архивов)

        Args:
            oil_check: Проверять ли условие остановки по нефти.

        Returns:
            True, если сработало условие остановки, иначе False.
        """
        if self.daily_run_limit_triggered():
            return True

        # OCR-проверку можно выполнять только на экране кампании Архивов
        if self.appear(WAR_ARCHIVES_CAMPAIGN_CHECK, offset=(20, 20)):
            # Проверяем, исчерпаны ли ключи данных
            current, remain, total = DATA_KEY_CAMPAIGN.ocr(self.device.image)
            logger.info(f'[Архивы] Ключи данных: {current} / {total}, осталось: {current}')
            if remain == total:
                logger.hr('[Архивы] Ключи данных исчерпаны')
                # Откладывать задачу можно только после исчерпания ключей данных
                self.config.task_delay(server_update=True)
                return True

        # В остальных случаях проверяем общие условия остановки
        return super().triggered_stop_condition(oil_check)

    def can_use_auto_search_continue(self):
        """Проверка возможности продолжения боя через автопоиск.

        Меню автопоиска имеет размытый фон, перекрывающий область OCR DATA_KEY_CAMPAIGN,
        поэтому в военных архивах функция продолжения боя через автопоиск отключена.

        Returns:
            Всегда возвращает False: продолжение через автопоиск не поддерживается.
        """
        return False

    def run(self, name=None, folder='campaign_main', mode='normal', total=0):
        """Выполнение кампании военных архивов.

        Принудительно включает использование Ключей данных и затем вызывает логику запуска кампании базового класса.
        Для входа в военные архивы обязательно требуются Ключи данных.

        Pages:
            in: page_archives (экран выбора военных архивов)
            out: page_main (главный экран после завершения задачи)

        Args:
            name: Название кампании, например 'war_archives_20190321_en'.
            folder: Путь к папке кампании, по умолчанию 'campaign_main'.
            mode: Режим кампании, 'normal' или 'hard'.
            total: Общее количество запусков, 0 означает без ограничений.
        """
        self.config.override(USE_DATA_KEY=True)
        self.daily_run_limit_reset()
        super().run(name, folder, mode, total)
