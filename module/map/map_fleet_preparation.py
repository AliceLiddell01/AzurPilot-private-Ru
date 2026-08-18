"""Управление экраном подготовки флота перед входом в кампанию.

Модуль отвечает за выбор и переключение флотов через выпадающие списки,
рекомендацию состава, очистку слотов, проверку ограничений сложного режима и
настройку ролей флотов для автопоиска.

``FleetOperator`` инкапсулирует операции одного слота флота: выбор, очистку,
активацию и проверку состояния. Базовый ``InfoHandler`` обрабатывает всплывающие
окна на экране подготовки.
"""

import numpy as np
from scipy import signal

from module.base.button import Button
from module.base.timer import Timer
from module.base.utils import *
from module.exception import GameStuckError, HardNotSatisfied
from module.handler.assets import AUTO_SEARCH_SET_MOB, AUTO_SEARCH_SET_BOSS, \
    AUTO_SEARCH_SET_ALL, AUTO_SEARCH_SET_STANDBY, \
    AUTO_SEARCH_SET_SUB_AUTO, AUTO_SEARCH_SET_SUB_STANDBY
from module.handler.info_handler import InfoHandler
from module.logger import logger
from module.map.assets import *


class FleetOperator:
    """Оператор одного слота флота на экране подготовки.

    Управляет выбором, рекомендацией и проверкой состояния одного слота.

    Атрибуты:
        FLEET_BAR_SHAPE_Y: высота строки выбора флота в пикселях.
        FLEET_BAR_MARGIN_Y: вертикальный интервал между строками.
        FLEET_BAR_ACTIVE_STD: порог стандартного отклонения активной строки.
        FLEET_IN_USE_STD: порог стандартного отклонения занятого слота.
    """
    FLEET_BAR_SHAPE_Y = 33
    FLEET_BAR_MARGIN_Y = 9
    FLEET_BAR_ACTIVE_STD = 45  # Активный слот: около 67; неактивный: около 12.
    FLEET_IN_USE_STD = 27  # Занятый слот: около 52; пустой: около 3–6.

    OFFSET = (-20, -80, 20, 5)

    def __init__(self, choose, advice, bar, clear, in_use, hard_satisfied, main):
        """Инициализировать оператор слота флота.

        Аргументы:
            choose (Button): кнопка открытия или закрытия списка выбора.
            advice (Button): кнопка рекомендации кораблей.
            bar (Button): область выпадающего списка флотов.
            clear (Button): кнопка очистки текущего флота.
            in_use (Button): область определения занятого слота.
            hard_satisfied (Button): область проверки ограничений сложного режима.
            main (InfoHandler): основной модуль Alas.
        """
        self._choose = choose
        self._advice = advice
        self._bar = bar
        self._clear = clear
        self._in_use = in_use
        self._hard_satisfied = hard_satisfied
        self.main = main

        if main.appear(clear, offset=FleetOperator.OFFSET):
            choose.load_offset(clear)
            bar.load_offset(clear)
            in_use.load_offset(clear)
            hard_satisfied.load_offset(clear)

    def __str__(self):
        return str(self._choose)[:-7]

    def parse_fleet_bar(self, image):
        """Получить номера выбранных флотов из изображения списка.

        Аргументы:
            image (np.ndarray): изображение выпадающего списка.

        Возвращает:
            list: номера выбранных флотов в диапазоне от 1 до 6.
        """
        width, height = image_size(image)
        result = []
        for index, y in enumerate(range(0, height, self.FLEET_BAR_SHAPE_Y + self.FLEET_BAR_MARGIN_Y)):
            area = (0, y, width, y + self.FLEET_BAR_SHAPE_Y)
            mean = get_color(image, area)
            if np.std(mean, ddof=1) > self.FLEET_BAR_ACTIVE_STD:
                result.append(index + 1)
        logger.info('[Карта — построение] Текущий выбор: %s' % str(result))
        return result

    def get_button(self, index):
        """Получить ``Button`` строки заданного флота в выпадающем списке.

        Аргументы:
            index (int): номер флота от 1 до 6.

        Возвращает:
            Button: кнопка соответствующей строки.
        """
        bar = self._bar.button
        area = area_offset(area=(
            0,
            (self.FLEET_BAR_SHAPE_Y + self.FLEET_BAR_MARGIN_Y) * (index - 1),
            bar[2] - bar[0],
            (self.FLEET_BAR_SHAPE_Y + self.FLEET_BAR_MARGIN_Y) * (index - 1) + self.FLEET_BAR_SHAPE_Y
        ), offset=(bar[0:2]))
        return Button(area=(), color=(), button=area, name='%s_INDEX_%s' % (str(self._bar), str(index)))

    def allow(self):
        """Вернуть ``True``, если текущий слот флота доступен для выбора."""
        return self.main.appear(self._clear, offset=FleetOperator.OFFSET)

    def is_hard(self):
        """Определить, относится ли экран к кампании сложного режима."""
        return self.main.appear(self._advice, offset=FleetOperator.OFFSET)

    def is_hard_satisfied(self):
        """Проверить выполнение ограничений сложного режима по оранжевым линиям.

        Наличие линий означает, что карта задаёт ограничения характеристик и
        текущий флот удовлетворяет хотя бы одному из них.

        Возвращает:
            bool: выполнены ли ограничения; ``None`` для несложного режима.
        """
        if not self.is_hard():
            return None

        area = self._hard_satisfied.button
        image = color_similarity_2d(self.main.image_crop(area, copy=False), color=(249, 199, 0))
        height = cv2.reduce(image, 1, cv2.REDUCE_AVG).flatten()
        parameters = {'height': 180, 'distance': 5}
        peaks, _ = signal.find_peaks(height, **parameters)
        lines = len(peaks)
        return lines > 0

    def raise_hard_not_satisfied(self):
        if self.is_hard_satisfied() is False:
            stage = self.main.config.Campaign_Name
            logger.critical(f'[Карта] Этап "{stage}" относится к сложному режиму; '
                            f'подготовьте флот "{str(self)}" в игре перед запуском Alas')
            raise HardNotSatisfied

    def clear(self, skip_first_screenshot=True):
        """Очистить выбранный флот идемпотентно."""
        main = self.main
        click_timer = Timer(3, count=6)
        empty_confirm = Timer(0.5, count=3).clear()
        blocked_confirm = Timer(2, count=4).clear()
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                main.device.screenshot()

            # При очистке флотов сложного режима может появиться подтверждение.
            # Сбрасываем доказательство пустого слота, чтобы не принять переходный
            # кадр за завершённую очистку.
            if self.main.handle_popup_confirm(str(self._clear)):
                empty_confirm.clear()
                blocked_confirm.clear()
                continue

            in_use = self.in_use()
            if not in_use:
                blocked_confirm.clear()
                if not empty_confirm.started():
                    empty_confirm.start()
                if empty_confirm.reached():
                    break
                continue

            empty_confirm.clear()

            # Если слот выглядит занятым, но CLEAR не виден, пустота не доказана.
            # Продолжаем ждать: штатный детектор зависания завершит путь безопасно,
            # вместо клика по неподтверждённому элементу.
            if not self.allow():
                if not blocked_confirm.started():
                    blocked_confirm.start()
                if blocked_confirm.reached():
                    raise GameStuckError(
                        '[Карта — построение] Занятый слот флота устойчиво не показывает кнопку очистки'
                    )
                continue

            blocked_confirm.clear()
            if click_timer.reached():
                main.device.click(self._clear)
                click_timer.reset()

    def recommend(self, skip_first_screenshot=True):
        """Заполнить слот рекомендованным флотом."""
        main = self.main
        click_timer = Timer(3, count=6)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                main.device.screenshot()

            # Завершение.
            if self.in_use():
                break

            # Нажатие.
            if click_timer.reached():
                main.device.click(self._choose)
                click_timer.reset()

    def open(self, skip_first_screenshot=True):
        """Открыть выпадающий список выбора флота."""
        main = self.main
        click_timer = Timer(3, count=6)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                main.device.screenshot()

            # Завершение.
            if self.bar_opened():
                break

            # Нажатие.
            if click_timer.reached():
                main.device.click(self._choose)
                click_timer.reset()

    def close(self, skip_first_screenshot=True):
        """Закрыть выпадающий список выбора флота."""
        main = self.main
        click_timer = Timer(3, count=6)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                main.device.screenshot()

            # Завершение.
            if not self.bar_opened():
                break

            # Нажатие.
            if click_timer.reached():
                main.device.click(self._choose)
                click_timer.reset()

    def click(self, index, skip_first_screenshot=True):
        """Выбрать флот в списке и закрыть выпадающее меню.

        Аргументы:
            index (int): номер флота от 1 до 6.
            skip_first_screenshot (bool): пропустить ли первый снимок экрана.
        """
        main = self.main
        button = self.get_button(index)
        click_timer = Timer(3, count=6)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                main.device.screenshot()

            if not self.bar_opened():
                # Завершение.
                if self.in_use():
                    break
                else:
                    self.open()

            # Нажатие.
            if click_timer.reached():
                main.device.click(button)
                click_timer.reset()

    def selected(self):
        """Вернуть номера выбранных флотов в диапазоне от 1 до 6."""
        data = self.parse_fleet_bar(self.main.image_crop(self._bar.button, copy=False))
        return data

    def in_use(self):
        """Вернуть ``True``, если в слоте выбран какой-либо флот."""
        # Обрезаем область FLEET_*_IN_USE, чтобы не захватывать информационную
        # панель автопоиска и не тратить время на её отдельную обработку.
        image = self.main.image_crop(self._in_use.button, copy=False)

        # Особая обработка скина Perseus с почти однородным цветом.
        # https://github.com/LmeSzinc/AzurLaneAutoScript/issues/5678
        # Для пустого слота характерен цвет (71, 70, 63).
        color = cv2.mean(image)[:3]
        if color_similar(color, (224, 154, 114), threshold=30):
            return True

        gray = rgb2gray(image)
        return np.std(gray.flatten(), ddof=1) > self.FLEET_IN_USE_STD

    def bar_opened(self):
        """Вернуть ``True``, если выпадающий список открыт."""
        # Проверяем яркость крайнего правого столбца области списка.
        luma = rgb2gray(self.main.image_crop(self._bar.button, copy=False))[:, -1]
        # Для FLEET_PREPARATION типичен диапазон яркости около 146–155.
        return np.sum(luma > 168) / luma.size > 0.5

    def ensure_to_be(self, index):
        """Установить конкретный флот.

        Аргументы:
            index (int): номер флота от 1 до 6.
        """
        self.open()
        if index in self.selected():
            self.close()
        else:
            self.click(index)


class FleetPreparation(InfoHandler):
    map_fleet_checked = False
    map_is_hard_mode = False

    def fleet_preparation(self, skip_first_screenshot=True):
        """При необходимости заменить выбранные флоты.

        Возвращает:
            bool: выполнялась ли замена.
        """
        logger.info(f'[Карта — построение] Используются флоты: {[self.config.Fleet_Fleet1, self.config.Fleet_Fleet2, self.config.Submarine_Fleet]}')
        if self.map_fleet_checked:
            return False

        # При пропуске подготовки доверяем текущим предварительно выбранным в игре
        # флотам и не открываем выпадающие списки. Это необходимо для аккаунтов,
        # у которых ещё разблокированы не все слоты флота.
        if self.config.Fleet_SkipPreparation:
            logger.info('[Карта — построение] Подготовка флота пропущена (Fleet_SkipPreparation=True); '
                        'используется заранее выбранный в игре флот')
            return True

        if self.appear(FLEET_1_CLEAR, offset=FleetOperator.OFFSET):
            AUTO_SEARCH_SET_MOB.load_offset(FLEET_1_CLEAR)
            AUTO_SEARCH_SET_BOSS.load_offset(FLEET_1_CLEAR)
            AUTO_SEARCH_SET_ALL.load_offset(FLEET_1_CLEAR)
            AUTO_SEARCH_SET_STANDBY.load_offset(FLEET_1_CLEAR)
        if self.appear(SUBMARINE_CLEAR, offset=FleetOperator.OFFSET):
            AUTO_SEARCH_SET_SUB_AUTO.load_offset(SUBMARINE_CLEAR)
            AUTO_SEARCH_SET_SUB_STANDBY.load_offset(SUBMARINE_CLEAR)

        fleet_1 = FleetOperator(
            choose=FLEET_1_CHOOSE, advice=FLEET_1_ADVICE, bar=FLEET_1_BAR, clear=FLEET_1_CLEAR,
            in_use=FLEET_1_IN_USE, hard_satisfied=FLEET_1_HARD_SATIESFIED, main=self)
        y = FLEET_1_CLEAR.button[1] - FLEET_1_CLEAR.area[1]
        if y < -10:
            logger.info('[Карта — построение] FLEET_1_CLEAR перемещён выше; загружены ресурсы W15')
            in_use = FLEET_2_IN_USE_W15
        else:
            in_use = FLEET_2_IN_USE
        fleet_2 = FleetOperator(
            choose=FLEET_2_CHOOSE, advice=FLEET_2_ADVICE, bar=FLEET_2_BAR, clear=FLEET_2_CLEAR,
            in_use=in_use, hard_satisfied=FLEET_2_HARD_SATIESFIED, main=self)
        submarine = FleetOperator(
            choose=SUBMARINE_CHOOSE, advice=SUBMARINE_ADVICE, bar=SUBMARINE_BAR, clear=SUBMARINE_CLEAR,
            in_use=SUBMARINE_IN_USE, hard_satisfied=SUBMARINE_HARD_SATIESFIED, main=self)

        # Проверяем подготовку кораблей для сложного режима.
        h1, h2, h3 = fleet_1.is_hard_satisfied(), fleet_2.is_hard_satisfied(), submarine.is_hard_satisfied()
        logger.info(f'[Карта — построение] Требования сложного режима: флот 1: {h1}, флот 2: {h2}, подлодка: {h3}')
        if self.config.SERVER in ['cn', 'en', 'jp']:
            if self.config.Fleet_Fleet1:
                fleet_1.raise_hard_not_satisfied()
            if self.config.Fleet_Fleet2:
                fleet_2.raise_hard_not_satisfied()
            if self.config.Submarine_Fleet:
                submarine.raise_hard_not_satisfied()

        # В сложном режиме ручная подготовка обычных флотов не требуется.
        self.map_is_hard_mode = h1 is not None or h2 is not None or h3 is not None
        if self.map_is_hard_mode:
            logger.info('[Карта — построение] Сложная кампания: подготовка флота не требуется')
            # Очищаем подлодку, если пользователь не настроил подводный флот.
            if submarine.allow():
                if self.config.Submarine_Fleet:
                    pass
                else:
                    submarine.clear()
            else:
                self.config.SUBMARINE = 0
            return False

        # Запоминаем доступность подлодки до настройки второго флота: раскрытый
        # второй слот может перекрыть кнопки подлодки и дать противоречивый результат.
        map_allow_submarine = submarine.allow()
        logger.attr('Подлодки разрешены', map_allow_submarine)
        if map_allow_submarine:
            if self.config.Submarine_Fleet:
                if fleet_2.allow():
                    self.device.click(fleet_2._clear)
                    # Новый снимок здесь не нужен: проверка подлодки не зависит от области флота 2.
                submarine.ensure_to_be(self.config.Submarine_Fleet)
            else:
                # Быстрее очищаем подлодку и второй флот простыми нажатиями;
                # окончательный результат затем подтверждают вызовы ``clear()``.
                op = False
                if fleet_2.allow():
                    self.device.click(fleet_2._clear)
                    op = True
                if submarine.allow():
                    self.device.click(submarine._clear)
                    op = True
                if op:
                    self.device.screenshot()

        # Не отключаем FLEET_2 по одному отсутствию кнопки: так можно ошибочно
        # очистить второй флот. Его состояние уточняется в конфигурации карты.

        if self.config.Fleet_Fleet2:
            # Используются оба флота. Выставляем их повторно, поскольку порядок
            # флотов в игре больше не определяется только меньшим номером слота.
            fleet_2.clear()
            fleet_1.ensure_to_be(self.config.Fleet_Fleet1)
            fleet_2.ensure_to_be(self.config.Fleet_Fleet2)
        else:
            # Второй флот не используется.
            if fleet_2.allow():
                fleet_2.clear()
            fleet_1.ensure_to_be(self.config.Fleet_Fleet1)

        # Повторно подтверждаем пустой слот подлодки после настройки флотов.
        if map_allow_submarine:
            if self.config.Submarine_Fleet:
                pass
            else:
                submarine.clear()
        else:
            self.config.SUBMARINE = 0

        return True
