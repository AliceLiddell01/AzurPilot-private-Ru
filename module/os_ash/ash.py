"""Модуль маяков Пепла (Ash / Ember) в Operation Siren.

Обеспечивает автоматизацию системы маяков Пепла (Ash / Ember / META) в Operation Siren (Azur Lane), включая:
- считывание прогресса сбора данных маяка через OCR и оценку статуса;
- отслеживание достижения ежедневного лимита сбора маяков;
- специальную обработку боёв с маяками (статус боя, подготовка, пропуск опыта);
- обработку исключения завершения маяка (AshBeaconFinished);
- автоматическое планирование задачи атаки маяка (запуск OpsiAshBeacon при сборе >= 100).

Система маяков Пепла позволяет собирать координаты для вызова боссов уровня META
с последующим получением чертежей и фрагментов персонажей META.
"""
from datetime import timedelta

from module.base.utils import image_left_strip
from module.combat.combat import BATTLE_PREPARATION, Combat
from module.config.time_source import now as current_time
from module.config.utils import DEFAULT_TIME
from module.logger import logger
from module.ocr.ocr import DigitCounter
from module.os_ash.assets import *
from module.os_handler.map_event import MapEventHandler
from module.ui.assets import BACK_ARROW
from module.ui.ui import UI


class DailyDigitCounter(DigitCounter):
    """Ежедневный счётчик с обрезкой левой границы изображения для устранения шумов."""

    def pre_process(self, image):
        image = super().pre_process(image)
        image = image_left_strip(image, threshold=120, length=35)
        return image


class AshBeaconFinished(Exception):
    """Сигнальное исключение завершения боя с маяком."""
    pass


class AshCombat(Combat):
    """Обработчик боя с маяком Пепла.

    Наследует Combat, переопределяя логику под специфику сражений META:
    - пользовательскую обработку экрана статуса боя (с учётом сохранения наград);
    - пропуск информации об опыте (бои META не дают опыта кораблям);
    - проверку состояния маяка на экране подготовки (завершён / пуст / уже на экране противостояния);
    - перехват исключения AshBeaconFinished при выполнении боя для штатного выхода.

    Когда маяк завершён или пуст, выбрасывает исключение AshBeaconFinished для остановки цикла боя.
    """

    def handle_battle_status(self, drop=None):
        """
        Обработать экран завершения боя и нажать подтверждение на экране расчёта.

        Args:
            drop (DropImage): Обработчик изображений дропа.

        Returns:
            bool: Было ли выполнено действие.
        """
        if self.is_combat_executing():
            return False
        if self.appear(BATTLE_STATUS, offset=(120, 20), interval=self.battle_status_click_interval):
            if drop:
                drop.handle_add(self)
            else:
                self.device.sleep((0.25, 0.5))
            self.device.click(BATTLE_STATUS)
            return True
        if self.appear(BATTLE_PREPARATION, offset=(30, 30), interval=2):
            self.device.click(BACK_ARROW)
            return True
        if super().handle_battle_status(drop=drop):
            return True

        return False

    def handle_exp_info(self):
        """
        В боях META опыт не выпадает, обработка информации об опыте не требуется.

        Случайный фон на BATTLE_STATUS может ложно активировать EXP_INFO_B, поэтому он игнорируется.
        """
        return False

    def handle_battle_preparation(self):
        """
        Обработать экран подготовки к бою и нажать кнопку начала сражения.

        Если маяк уже завершён или пуст, выбрасывает исключение AshBeaconFinished.

        Returns:
            bool: Было ли выполнено действие.
        """
        if super().handle_battle_preparation():
            return True

        if self.appear_then_click(ASH_START, offset=(30, 30), interval=2):
            return True
        if self.handle_get_items():
            return True
        if self.appear(BEACON_REWARD):
            logger.info("[META — бой] Маяк завершён")
            raise AshBeaconFinished
        if self.appear(BEACON_EMPTY, offset=(20, 20)):
            logger.info("[META — бой] Маяк пуст")
            raise AshBeaconFinished
        if self.appear(ASH_SHOWDOWN, offset=(20, 20)):
            logger.info("[META — бой] Уже открыт экран противостояния META")
            raise AshBeaconFinished

        return False

    def combat(self, *args, expected_end=None, **kwargs):
        """
        Провести бой с перехватом исключения завершения маяка для штатного выхода.

        Args:
            expected_end: Функция проверки завершения боя.
        """
        try:
            super().combat(*args, expected_end=expected_end, **kwargs)
        except AshBeaconFinished:
            pass


class OSAsh(UI, MapEventHandler):
    """Модуль маяков Пепла в Operation Siren.

    Отвечает за распознавание прогресса сбора маяков и автоматический запуск задачи атаки маяка.

    Рабочий процесс:
    1. Через OCR считывает прогресс сбора маяка (DigitCounter).
    2. Определяет статус: доступно для сбора / не собрано до конца / достигнут лимит / перекрыто.
    3. При прогрессе сбора >= 100 и доступности задачи запускает задачу OpsiAshBeacon.
    4. Проверяет время следующего запуска маяка: запуск разрешён, если до него больше 30 минут.

    Attributes:
        _ash_fully_collected (bool): Собраны ли все данные маяка (достигнут дневной лимит или предел хранения).
    """
    _ash_fully_collected = False

    def ash_collect_status(self):
        """
        Считать прогресс сбора маяка Пепла через OCR.

        Returns:
            int: Значение прогресса сбора от 0 до 100.
        """
        if self._ash_fully_collected:
            return 0
        if self.image_color_count(ASH_COLLECT_STATUS, color=(235, 235, 235), threshold=221, count=20):
            logger.info('[META — бой] Состояние маяка: данные можно собрать')
            ocr_collect = DigitCounter(
                ASH_COLLECT_STATUS, letter=(235, 235, 235), threshold=160, name='OCR_ASH_COLLECT_STATUS')
            ocr_daily = DailyDigitCounter(
                ASH_DAILY_STATUS, letter=(235, 235, 235), threshold=160, name='OCR_ASH_DAILY_STATUS')
        elif self.image_color_count(ASH_COLLECT_STATUS, color=(140, 142, 140), threshold=221, count=20):
            logger.info('[META — бой] Состояние маяка: данные собраны не полностью')
            ocr_collect = DigitCounter(
                ASH_COLLECT_STATUS, letter=(140, 142, 140), threshold=160, name='OCR_ASH_COLLECT_STATUS')
            ocr_daily = DailyDigitCounter(
                ASH_DAILY_STATUS, letter=(140, 142, 140), threshold=160, name='OCR_ASH_DAILY_STATUS')
        else:
            # При получении или завершении ежедневных заданий+ Операции «Сирена» всплывающее окно перекрывает состояние маяка
            logger.info('[META — бой] Состояние маяка перекрыто, повторная проверка позже')
            return 0

        status, _, _ = ocr_collect.ocr(self.device.image)
        daily, _, _ = ocr_daily.ocr(self.device.image)

        if daily >= 200:
            logger.info('[META — бой] Все данные маяка на сегодня собраны')
            self._ash_fully_collected = True
        elif status >= 200:
            logger.info('[META — бой] Достигнут предел хранения данных маяка')
            self._ash_fully_collected = True

        if status < 0:
            status = 0
        return status

    def _support_call_ash_beacon_task(self):
        """
        Проверить, разрешён ли запуск задачи маяка.

        Вызов разрешён, если до следующего запланированного времени запуска остаётся более 30 минут.

        Returns:
            bool: Поддерживается ли вызов задачи маяка.
        """
        # Время следующего запуска задачи маяка
        next_run = self.config.cross_get(keys="OpsiAshBeacon.Scheduler.NextRun", default=DEFAULT_TIME)
        # До следующего запуска остаётся больше 30 минут
        if next_run - current_time() > timedelta(minutes=30):
            return True
        return False

    def handle_ash_beacon_attack(self):
        """
        Проверить статус сбора данных маяка и при выполнении условий запустить задачу атаки.

        При прогрессе сбора >= 100 и готовности планировщика запускает OpsiAshBeacon.

        Returns:
            bool: Была ли запущена атака маяка.

        Pages:
            in: is_in_map
            out: is_in_map
        """
        if self.config.is_task_enabled('OpsiAshBeacon') \
                and self.ash_collect_status() >= 100 \
                and self._support_call_ash_beacon_task():
            self.config.task_call(task='OpsiAshBeacon')
            return True

        return False
