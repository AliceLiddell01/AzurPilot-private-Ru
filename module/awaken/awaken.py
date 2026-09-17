"""
Модуль пробуждения кораблей (Awaken).

Автоматизирует процесс пробуждения кораблей, повышая их максимальный уровень со 100 до 120 или 125.

Основные функции:
    - Определение текущего уровня корабля (100–125)
    - Проверка достаточности ресурсов для пробуждения (монеты, Cognitive Chip, Cognitive Array)
    - Выполнение однократной операции пробуждения, включая подтверждение и ожидание анимации
    - Циклическое пробуждение одного корабля до достижения предела уровня или нехватки ресурсов
    - Обход всех доступных для пробуждения кораблей в доке до исчерпания ресурсов

Механика уровней пробуждения:
    - Обычное пробуждение: расходует монеты + Cognitive Chip, предел уровня — 120
    - Пробуждение+ (Awaken+): дополнительно расходует Cognitive Array, предел уровня — 125
    - Сначала выполняется Пробуждение+ (с Cognitive Array), затем обычное пробуждение (с Cognitive Chip)

Логика проверки ресурсов:
    - Определение наличия ресурсов через сопоставление кнопок и распознавание красного цвета текста
    - Если кнопка COST_ARRAY отсутствует, кнопки COST_COIN и COST_CHIP сдвигаются вправо на 54px
    - Корректность результата валидируется с учётом возможного смещения кнопок

Наследование:
    Наследуется от Dock (операции с доком) и использует фильтры дока для отбора доступных кораблей.

Pages:
    Экран пробуждения: is_in_awaken
    Экран дока: page_dock
"""

from module.awaken.assets import *
from module.base.timer import Timer
from module.exception import ScriptError
from module.logger import logger
from module.ocr.ocr import Digit
from module.retire.dock import DOCK_EMPTY, Dock
from module.ui.assets import BACK_ARROW
from module.ui.page import page_dock, page_main


class ShipLevel(Digit):
    """
    OCR-распознаватель уровня корабля.

    Выполняет постобработку стандартного Digit OCR, принимая только значения уровней в диапазоне 100–125.
    Результаты вне диапазона считаются некорректными и возвращают 0.
    """
    def after_process(self, result):
        result = super().after_process(result)
        if result < 100 or result > 125:
            logger.warning('[Пробуждение] Некорректный уровень корабля')
            result = 0
        return result


class Awaken(Dock):
    """
    Обработчик задачи пробуждения.

    Управляет полным циклом пробуждения кораблей, включая проверку ресурсов, выполнение пробуждения и обход дока.
    Наследуется от Dock для использования функций фильтрации, сортировки и выбора кораблей в доке.

    Основной рабочий процесс:
        1. Переход в док, фильтрация кораблей по избранному и доступности пробуждения
        2. Вход в детали корабля, выполнение пробуждения до предела уровня или нехватки ресурсов
        3. Выход из деталей корабля, переход к следующему
        4. Завершение при отсутствии подходящих кораблей или исчерпании ресурсов

    Атрибуты:
        Дополнительные атрибуты экземпляра отсутствуют, все состояния передаются через аргументы и возвращаемые значения методов.

    Параметры конфигурации:
        Awaken_LevelCap: Предел уровня пробуждения ('level120' или 'level125')
        Awaken_Favourite: Пробуждать ли только избранные корабли
    """
    def _get_button_state(self, button: Button):
        """
        Получение состояния указанной кнопки ресурса.

        Args:
            button: Кнопка COST_COIN, COST_CHIP или COST_ARRAY

        Returns:
            bool: True при достаточности ресурсов, False при нехватке; None, если данный ресурс не требуется
        """
        # Если COST_ARRAY отсутствует, COST_COIN и COST_CHIP смещаются вправо на 54px
        if button.match(self.device.image, offset=(75, 20)):
            # Look down, see if there are red letters
            area = button.button
            area = (area[0], area[3], area[2], area[3] + 60)
            if self.image_color_count(area, color=(214, 53, 33), threshold=180, count=16):
                return False
            else:
                return True
        else:
            return None

    def _get_awaken_cost(self, use_array=False):
        """
        Получение состояния ресурсов, необходимых для пробуждения.

        Args:
            use_array: True для пробуждения до 125 уровня, False для 120 уровня

        Returns:
            bool or str:
                True, если всех необходимых ресурсов достаточно,
                False, если хотя бы одного ресурса недостаточно,
                'unexpected_array', если Cognitive Array не планировался, но появился,
                'invalid', если состояние кнопок некорректно
        """
        coin = self._get_button_state(COST_COIN)
        chip = self._get_button_state(COST_CHIP)
        array = self._get_button_state(COST_ARRAY)

        logger.attr('Стоимость пробуждения', {'coin': coin, 'chip': chip, 'array': array})

        def is_right_moved(button):
            # Если COST_ARRAY отсутствует, COST_COIN и COST_CHIP смещаются вправо на 54px
            return button.button[0] - button.area[0] > 20

        # Проверяем корректность результата
        if array is not None:
            if not use_array:
                logger.warning('[Пробуждение] Cognitive Array доступен, хотя его использование отключено')
                return 'unexpected_array'
            # Если нужен Array, одновременно должны присутствовать Coin и Chip
            if coin is not None and not is_right_moved(COST_COIN) \
                    and chip is not None and not is_right_moved(COST_CHIP):
                result = coin and chip and array
                logger.attr('Ресурсов для пробуждения достаточно', result)
                return result
        else:
            # Если Array не нужен, Coin и Chip должны присутствовать и быть смещены вправо
            if coin is not None and is_right_moved(COST_COIN) \
                    and chip is not None and is_right_moved(COST_CHIP):
                result = coin and chip
                logger.attr('Ресурсов для пробуждения достаточно', result)
                return result

        logger.warning('[Пробуждение] Некорректное состояние стоимости пробуждения')
        return 'invalid'

    def handle_awaken_finish(self):
        return self.appear_then_click(AWAKEN_FINISH, offset=(20, 20), interval=1)

    def is_in_awaken(self):
        return SHIP_LEVEL_CHECK.match_luma(self.device.image, similarity=0.7)

    def awaken_popup_close(self, skip_first_screenshot=True):
        logger.info('[Пробуждение] Закрытие окна пробуждения')
        self.interval_clear(AWAKEN_CANCEL)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.is_in_awaken():
                break
            if self.appear_then_click(AWAKEN_CANCEL, offset=(20, 20), interval=3):
                continue
            if self.handle_awaken_finish():
                continue

    def awaken_once(self, use_array=False, skip_first_screenshot=True):
        """
        Выполнение однократной операции пробуждения.

        Args:
            use_array (bool): Использовать ли Cognitive Array (пробуждение до 125 уровня)
            skip_first_screenshot (bool): Пропускать ли первый снимок экрана

        Returns:
            str: Статус результата: 'no_exp', 'unexpected_array', 'insufficient', 'timeout', 'success'

        Pages:
            in: is_in_awaken
            out: is_in_awaken
        """
        logger.hr('Однократное пробуждение', level=2)
        interval = Timer(3, count=6)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear(AWAKEN_CONFIRM):
                break
            if LEVEL_UP.match_luma(self.device.image):
                logger.info(f'[Пробуждение] Однократное пробуждение завершено на {LEVEL_UP}')
                return 'no_exp'
            # Из-за случайного фона снижаем порог сходства
            if interval.reached() and AWAKENING.match_luma(self.device.image, similarity=0.7):
                self.device.click(AWAKENING)
                interval.reset()
                continue

        logger.info('[Пробуждение] Определение стоимости пробуждения')
        timeout = Timer(2, count=6).start()
        skip_first_screenshot = True
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            result = self._get_awaken_cost(use_array)
            if result == 'unexpected_array':
                # Эта ситуация возникать не должна
                self.awaken_popup_close()
                return result
            elif result is False:
                logger.info('[Пробуждение] Недостаточно ресурсов для пробуждения')
                self.awaken_popup_close()
                return 'insufficient'
            elif result is True:
                # Ресурсов достаточно
                break
            elif result == 'invalid':
                # Повторяем попытку, одновременно проверяя тайм-аут
                pass
            else:
                raise ScriptError(f'Неожиданный результат _get_awaken_cost: {result}')
            if timeout.reached():
                logger.warning('[Пробуждение] Тайм-аут определения стоимости пробуждения')
                self.awaken_popup_close()
                return 'timeout'

        # Ресурсов достаточно — подтверждаем пробуждение
        logger.info('[Пробуждение] Подтверждение пробуждения')
        self.interval_clear(AWAKEN_CONFIRM)
        # При достаточном опыте окно пробуждения появляется только через 10 секунд, а закрытие кликом занимает 2 секунды
        # Поэтому здесь используется более длинный тайм-аут
        timeout = Timer(30, count=30).start()
        finished = False
        skip_first_screenshot = True
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            # Условие завершения
            if timeout.reached():
                logger.warning('[Пробуждение] Тайм-аут подтверждения пробуждения')
                self.awaken_popup_close()
                break
            if finished and self.is_in_awaken():
                logger.info('[Пробуждение] Пробуждение завершено')
                break
            # Выполняем клики
            if self.appear_then_click(AWAKEN_CONFIRM, offset=(20, 20), interval=3):
                continue
            if self.handle_popup_confirm('AWAKEN'):
                continue
            if self.handle_awaken_finish():
                finished = True
                continue

        self.device.click_record_clear()
        return 'success'

    def get_ship_level(self, skip_first_screenshot=True):
        """
        Получение уровня текущего корабля.

        Args:
            skip_first_screenshot (bool): Пропускать ли первый снимок экрана

        Returns:
            int: Уровень корабля 100–125; 0 при ошибке
        """
        ocr = ShipLevel(OCR_SHIP_LEVEL, letter=(255, 255, 255), threshold=128, name='ShipLevel')
        timeout = Timer(2, count=4).start()
        level = 0
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.is_in_awaken():
                level = ocr.ocr(self.device.image)
                if level > 0:
                    return level
            if timeout.reached():
                logger.warning('[Пробуждение] Тайм-аут определения уровня корабля')
                return level

    def awaken_ship(self, use_array=False, skip_first_screenshot=True):
        """
        Выполнение пробуждения одного корабля до исчерпания опыта или достижения целевого уровня.

        Args:
            use_array (bool): True для пробуждения до 125 уровня, False для 120 уровня
            skip_first_screenshot (bool): Пропускать ли первый снимок экрана

        Returns:
            str: 'level_max', 'insufficient', 'no_exp', 'timeout'

        Pages:
            in: is_in_awaken
            out: is_in_awaken
        """
        logger.hr('Пробуждение корабля', level=1)
        logger.info(f'[Пробуждение] Пробуждение корабля, использовать Cognitive Array={use_array}')

        if use_array:
            stop_level = 125
        else:
            stop_level = 120

        if not skip_first_screenshot:
            self.device.screenshot()

        for _ in range(7):
            level = self.get_ship_level()
            if level > 0:
                if level >= stop_level:
                    logger.info(f'[Пробуждение] Пробуждение корабля завершено на целевом уровне')
                    return 'level_max'
                else:
                    result = self.awaken_once(use_array)
                    # 'no_exp'、'unexpected_array'、'insufficient'、'timeout'、'success'
                    if result == 'success':
                        continue
                    if result in ['insufficient', 'no_exp']:
                        # Сразу возвращаем исходный результат
                        return result
                    if result == 'unexpected_array':
                        # Возможно, по ошибке открыт экран подтверждения пробуждения; повторный awaken_once выполнит проверку заново
                        continue
                    if result == 'timeout':
                        # Тайм-аут получения ресурсов; повторная попытка должна исправить ситуацию
                        continue
                    raise ScriptError(f'Неожиданный результат awaken_once: {result}')
            else:
                # Тайм-аут получения уровня — запрашиваем выход
                return 'timeout'

        # Ошибка — запрашиваем выход
        logger.warning('[Пробуждение] Слишком много попыток пробуждения одного корабля')
        return 'timeout'

    def awaken_exit(self, skip_first_screenshot=True):
        """
        Выход из интерфейса пробуждения и возврат в док.

        Pages:
            in: is_in_awaken
            out: DOCK_CHECK
        """
        logger.info('[Пробуждение] Выход из экрана пробуждения')
        interval = Timer(3)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.ui_page_appear(page_dock):
                logger.info(f'[Пробуждение] Выход из экрана пробуждения на {page_dock}')
                break
            if interval.reached() and self.is_in_awaken():
                logger.info(f'[Пробуждение] На экране пробуждения -> {BACK_ARROW}')
                self.device.click(BACK_ARROW)
                interval.reset()
                continue
            if self.handle_awaken_finish():
                continue
            if self.appear_then_click(AWAKEN_CANCEL, offset=(20, 20), interval=3):
                continue
            if self.is_in_main(interval=5):
                self.device.click(page_main.links[page_dock])
                continue

    def awaken_run(self, use_array=False, favourite=False):
        """
        Пробуждение всех доступных кораблей в доке до исчерпания ресурсов.

        Args:
            use_array (bool): True для пробуждения до 125 уровня, False для 120 уровня
            favourite (bool): True для пробуждения только избранных кораблей, False для всех

        Returns:
            str: 'insufficient', 'finish', 'timeout'

        Pages:
            in: Любая страница
            out: page_dock
        """
        logger.hr('Цикл пробуждения', level=1)
        self.ui_ensure(page_dock)
        self.dock_favourite_set(enable=favourite, wait_loading=False)
        self.dock_sort_method_dsc_set(wait_loading=False)
        if use_array:
            extra = ['can_awaken_plus']
        else:
            extra = ['can_awaken']
        self.dock_filter_set(extra=extra)

        while 1:
            # На странице page_dock
            if self.appear(DOCK_EMPTY, offset=(20, 20)):
                logger.info('[Пробуждение] Цикл завершён: нет кораблей для пробуждения')
                result = 'finish'
                break

            # page_dock -> SHIP_DETAIL_CHECK
            entered = self.dock_enter_first()
            if not entered:
                logger.info('[Пробуждение] Цикл завершён: нет кораблей для пробуждения')
                result = 'finish'
                break

            # На странице is_in_awaken
            result = self.awaken_ship(use_array)
            self.awaken_exit()
            # 'insufficient'、'no_exp'、'timeout'
            if result in ['no_exp', 'level_max']:
                # Awaken next ship
                continue
            if result == 'insufficient':
                logger.info('[Пробуждение] Цикл завершён: ресурсы исчерпаны')
                break
            if result == 'timeout':
                logger.info(f'[Пробуждение] Цикл завершён, результат={result}')
                break
            raise ScriptError(f'Неожиданный результат awaken_ship: {result}')

        return result

    def run(self):
        # Сначала выполняем Awaken+ с использованием Cognitive Array
        favourite = self.config.Awaken_Favourite
        if self.config.Awaken_LevelCap == 'level125':
            # Используем Cognitive Array
            result = self.awaken_run(use_array=True, favourite=favourite)
            # Используем Cognitive Chip
            if result != 'timeout':
                self.awaken_run(favourite=favourite)
        elif self.config.Awaken_LevelCap == 'level120':
            # Используем Cognitive Chip
            self.awaken_run(favourite=favourite)
        else:
            raise ScriptError(f'Неизвестное значение Awaken_LevelCap={self.config.Awaken_LevelCap}')

        # Сбрасываем фильтры дока
        logger.hr('Завершение цикла пробуждения', level=1)
        if favourite:
            self.dock_favourite_set(wait_loading=False)
        self.dock_filter_set(wait_loading=False)

        # Планируем следующий запуск
        self.config.task_delay(server_update=True)
