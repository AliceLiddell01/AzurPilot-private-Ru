"""
Модуль задачи учений (PvP).

Автоматически выполняет ежедневные операции в системе учений, включая:
- Распознавание через OCR оставшихся попыток учений и таймера сброса сезона
- Поддержку различных стратегий выбора соперника: по максимальному опыту, минимальной сложности, крайний левый и др.
- Поддержку настройки временного окна «Испытания адмирала» для концентрированного расхода попыток
- Учёт и управление количеством обновлений списка соперников с посуточным сбросом
- Поддержку отложенного выполнения ближе к окончанию сезона

Стратегии выбора соперника:
- max_exp: выбор соперника с наибольшим опытом
- easiest: выбор наиболее простого соперника
- easiest_else_exp: сначала наиболее простой, при невозможности победить — переключение на максимальный опыт
- leftmost: выбор крайнего левого соперника
"""
import datetime
from module.config.time_source import now as current_time
from module.config.utils import get_server_last_update
from module.exercise.assets import *
from module.exercise.combat import ExerciseCombat
from module.logger import logger
from module.ocr.ocr import Digit, Ocr, OcrYuv
from module.ui.page import page_exercise
from module.config.utils import get_server_next_update

class DatedDuration(Ocr):
    """
    OCR-распознаватель продолжительности с датой.

    Используется для распознавания формата оставшегося времени сезона учений, например `10d 01:30:30`.
    Исправляет частые ошибки OCR (I->1, D->0, S->5).

    Attributes:
        buttons: Область распознавания OCR.
        lang (str): Язык OCR, по умолчанию 'cnocr'.
        alphabet (str): Набор распознаваемых символов.
    """

    def __init__(self, buttons, lang='cnocr', letter=(255, 255, 255), threshold=128, alphabet='0123456789:IDS天日d',
                 name=None):
        super().__init__(buttons, lang=lang, letter=letter, threshold=threshold, alphabet=alphabet, name=name)

    def after_process(self, result):
        result = super().after_process(result)
        result = result.replace('I', '1').replace('D', '0').replace('S', '5')
        return result

    def ocr(self, image, direct_ocr=False):
        """
        Выполнение OCR-распознавания продолжительности с датой, например `10d 01:30:30`.

        Args:
            image: Изображение снимка экрана.
            direct_ocr: Выполнять ли прямое распознавание.

        Returns:
            datetime.timedelta или их список: объект временного интервала.
        """
        result_list = super().ocr(image, direct_ocr=direct_ocr)
        if not isinstance(result_list, list):
            result_list = [result_list]
        result_list = [self.parse_time(result) for result in result_list]
        if len(self.buttons) == 1:
            result_list = result_list[0]
        return result_list

    @staticmethod
    def parse_time(string):
        """
        Разбор строки продолжительности с датой.

        Args:
            string (str): Строка продолжительности, например `10d 01:30:30`.

        Returns:
            datetime.timedelta: Разобранный объект интервала времени.
        """
        import re
        result = re.search(r'(\d{1,2})\D?(\d{1,2}):?(\d{2}):?(\d{2})', string)
        if result:
            result = [int(s) for s in result.groups()]
            return datetime.timedelta(days=result[0], hours=result[1], minutes=result[2], seconds=result[3])
        else:
            logger.warning(f'[Учения — OCR] Недопустимая продолжительность с датой: {string}')
            return datetime.timedelta(days=0, hours=0, minutes=0, seconds=0)


class DatedDurationYuv(DatedDuration, OcrYuv):
    """
    OCR-распознаватель продолжительности с датой в цветовом пространстве YUV.

    Наследуется от DatedDuration и OcrYuv, используя цветовое пространство YUV для предобработки.
    """
    pass


OCR_EXERCISE_REMAIN = Digit(OCR_EXERCISE_REMAIN, letter=(173, 247, 74), threshold=128)
OCR_PERIOD_REMAIN = DatedDuration(OCR_PERIOD_REMAIN, letter=(255, 255, 255), threshold=128)
ADMIRAL_TRIAL_HOUR_INTERVAL = {
    # "aggressive": [336, 0]  # Агрессивный режим
    "sun18": [6, 0],
    "sun12": [12, 6],
    "sun0": [24, 12],
    "sat18": [30, 24],
    "sat12": [36, 30],
    "sat0": [48, 36],
    "fri18": [56, 48]
}


class Exercise(ExerciseCombat):
    """
    Основной обработчик задачи учений, отвечающий за планирование и выполнение боёв.

    Наследуется от ExerciseCombat, объединяя выбор соперника, бой и управление попытками.
    Автоматически выбирает соперников по заданной стратегии, поддерживает различные алгоритмы расхода попыток и отложенный запуск.

    Attributes:
        opponent_change_count (int): Текущее количество обновлений соперников (до 5 раз в день).
        remain (int): Оставшееся количество попыток учений.
        preserve (int): Резерв попыток, ниже которого выполнение останавливается.
    """

    opponent_change_count = 0
    remain = 0
    preserve = 0

    def _new_opponent(self):
        """
        Обновление списка соперников.

        Нажимает кнопку обновления для получения новых соперников и фиксирует суточный счётчик обновлений.
        """
        logger.info('[Учения — противник] Обновление списка противников')
        self.appear_then_click(NEW_OPPONENT)
        self.opponent_change_count += 1

        logger.attr('Количество обновлений противников', self.opponent_change_count)
        self.config.set_record(Exercise_OpponentRefreshValue=self.opponent_change_count)

        self.ensure_no_info_bar(timeout=3)

    def _opponent_fleet_check_all(self):
        """
        Проверка информации о флотах всех соперников.

        При режиме выбора leftmost проверка пропускается и сразу используется крайний левый соперник.
        """
        if self.config.Exercise_OpponentChooseMode != 'leftmost':
            super()._opponent_fleet_check_all()

    def _opponent_sort(self, method=None):
        """
        Сортировка соперников в соответствии со стратегией.

        Args:
            method (str): Метод сортировки; по умолчанию используется значение настройки Exercise_OpponentChooseMode.
                В режиме leftmost сразу возвращается [0, 1, 2, 3].

        Returns:
            list[int]: Список индексов соперников, отсортированный по приоритету.
        """
        if method is None:
            method = self.config.Exercise_OpponentChooseMode
        if method != 'leftmost':
            return super()._opponent_sort(method=method)
        else:
            return [0, 1, 2, 3]

    def _exercise_once(self):
        """
        Выполнение одного боя учений.

        Обрабатывает обновление списка соперников и поражения в боях.

        Returns:
            bool: True, если соперник побеждён; False, если ни одного соперника не удалось победить и попытки обновления исчерпаны.
        """
        self._opponent_fleet_check_all()
        while 1:
            for opponent in self._opponent_sort():
                logger.hr(f'Противник {opponent}', level=2)
                success = self._combat(opponent)
                if success:
                    return success

            if self.opponent_change_count >= 5:
                return False

            self._new_opponent()
            self._opponent_fleet_check_all()

    def _exercise_easiest_else_exp(self):
        """
        Приоритетный выбор простейшего соперника; при невозможности победить — переключение на максимальный опыт с принятием поражения.

        Обрабатывает обновление списка соперников и поражения в боях.

        Returns:
            bool: True, если соперник побеждён; False, если ни одного соперника не удалось победить и попытки обновления исчерпаны.
        """
        method = "easiest_else_exp"
        restore = self.config.Exercise_LowHpThreshold
        threshold = self.config.Exercise_LowHpThreshold
        self._opponent_fleet_check_all()
        while 1:
            opponents = self._opponent_sort(method=method)
            logger.hr(f'Противник {opponents[0]}', level=2)
            self.config.override(Exercise_LowHpThreshold=threshold)
            success = self._combat(opponents[0])
            if success:
                self.config.override(Exercise_LowHpThreshold=restore)
                return success
            else:
                if self.opponent_change_count < 5:
                    logger.info("[Учения — противник] Не удалось победить самого простого противника; обновление")
                    self._new_opponent()
                    self._opponent_fleet_check_all()
                    continue
                else:
                    logger.info("[Учения — противник] Не удалось победить самого простого противника; переключение на максимальный опыт")
                    method = "max_exp"
                    threshold = 0

    def _get_opponent_change_count(self):
        """
        Получение количества обновлений списка соперников.

        В течение одного дня счётчик равен последнему сохранённому значению или 6 (обновления больше не выполняются).
        В новый день счётчик сбрасывается в 0 (доступно до 5 обновлений).

        Returns:
            int: Текущее количество обновлений списка соперников.
        """
        record = self.config.Exercise_OpponentRefreshRecord
        update = get_server_last_update('00:00')
        if record.date() == update.date():
            # Тот же день
            return self.config.Exercise_OpponentRefreshValue
        else:
            # Новый день
            self.config.set_record(Exercise_OpponentRefreshValue=0)
            return 0

    def _get_exercise_reset_remain(self):
        """
        Получение оставшегося времени до сброса сезона учений.

        Returns:
            datetime.timedelta: Оставшееся время до сброса.
        """
        result = OCR_PERIOD_REMAIN.ocr(self.device.image)
        return result

    def _get_exercise_strategy(self):
        """
        Получение стратегии расхода попыток учений.

        На основе значения Exercise_ExerciseStrategy определяет число сохраняемых попыток и интервал времени адмиральского испытания.

        Returns:
            tuple: (preserve, admiral_interval)
                - preserve (int): Число сохраняемых попыток (0 в агрессивном режиме, 5 в консервативном).
                - admiral_interval (list или None): Интервал времени испытания [start, end] (в часах),
                  None для агрессивного режима.
        """
        if self.config.Exercise_ExerciseStrategy == "aggressive":
            preserve = 0
            admiral_interval = None
        else:
            preserve = 5
            admiral_interval = ADMIRAL_TRIAL_HOUR_INTERVAL[self.config.Exercise_ExerciseStrategy]

        return preserve, admiral_interval

    def run(self):
        """
        Основная точка входа задачи учений.

        Последовательность:
        1. Переход на страницу учений
        2. Получение количества обновлений списка соперников и стратегии расхода
        3. Проверка достижения интервала испытания и решение о принудительном расходе
        4. Проверка необходимости отложить выполнение
        5. Циклическое проведение боёв до исчерпания попыток или порога сохранения
        6. Планирование времени следующего запуска задачи

        Pages:
            in: Любая страница
            out: page_exercise
        """
        self.ui_ensure(page_exercise)
        server_update = self.config.Scheduler_ServerUpdate

        self.opponent_change_count = self._get_opponent_change_count()
        logger.attr('Количество обновлений противников', self.opponent_change_count)
        logger.attr('Стратегия расходования попыток учений', self.config.Exercise_ExerciseStrategy)
        self.preserve, admiral_interval = self._get_exercise_strategy()

        remain_time = OCR_PERIOD_REMAIN.ocr(self.device.image)
        logger.info(f'[Учения — планировщик] До конца сезона учений: {remain_time}')

        if admiral_interval is not None and remain_time:
            admiral_start, admiral_end = admiral_interval

            if admiral_start > int(remain_time.total_seconds() // 3600) >= admiral_end:  # Наступило заданное время адмиральского испытания
                logger.info('[Учения — планировщик] Наступило заданное время адмиральского испытания; расходуем все попытки')
                self.preserve = 0
                forced_run =True
            elif int(remain_time.total_seconds() // 3600) < 6:  # Даже если не выбран "sun18", расходуем попытки до 18:00 воскресенья
                logger.info('[Учения — планировщик] До конца сезона учений меньше 6 часов; расходуем все попытки')
                self.preserve = 0
                forced_run = True
            else:
                logger.info(f'[Учения — планировщик] Сохраняем {self.preserve} попыток учений')
                forced_run = False
        else:
            forced_run = False

        # Откладываем выполнение задачи до заданного времени
        if ((get_server_next_update(server_update) - current_time()).seconds >
            3600 * self.config.Exercise_DelayUntilHoursBeforeNextUpdate)\
                and not forced_run:
            logger.warning(f'[Учения — планировщик] Выполнить следует за {self.config.Exercise_DelayUntilHoursBeforeNextUpdate} '
                           f'ч. до следующего обновления; задача отложена')
            run = False
        else:
            run = True

        while run:
            self.remain = OCR_EXERCISE_REMAIN.ocr(self.device.image)
            if self.remain <= self.preserve:
                break

            logger.hr(f'Осталось попыток учений: {self.remain}', level=1)
            if self.config.Exercise_OpponentChooseMode == "easiest_else_exp":
                success = self._exercise_easiest_else_exp()
            else:
                success = self._exercise_once()
            if not success:
                logger.info('[Учения — противник] Попытки обновления противников исчерпаны')
                break

        # self.equipment_take_off_when_finished()

        # Планировщик
        with self.config.multi_set():
            self.config.set_record(Exercise_OpponentRefreshValue=self.opponent_change_count)
            if self.remain <= self.preserve or self.opponent_change_count >= 5:
                next_run = get_server_next_update(server_update) \
                           - datetime.timedelta(hours=self.config.Exercise_DelayUntilHoursBeforeNextUpdate)
                now = current_time()
                if next_run < now or run:
                    self.config.task_delay(server_update=True)
                    return
                minutes_to_delay = int((next_run - now).total_seconds() / 60 + 1)
                self.config.task_delay(minute=minutes_to_delay)
            else:
                self.config.task_delay(success=False)
