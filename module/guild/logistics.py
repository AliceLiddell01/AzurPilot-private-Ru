"""Модуль снабжения (логистики) гильдии.

Обрабатывает все операции на странице снабжения гильдии, включая:
- Получение регулярных припасов гильдии
- Принятие еженедельных заданий гильдии и сбор наград
- Обмен ресурсов гильдии на предметы

Модуль поддерживает особенности различных серверов (CN/EN/JP/TW),
используя декоратор `@Config.when(SERVER=...)` для логики определения статуса задач.

Префикс параметров конфигурации: `GuildLogistics_*`.
"""

import re

from module.base.button import ButtonGrid
from module.base.decorator import Config, cached_property
from module.base.filter import Filter
from module.base.timer import Timer
from module.base.utils import *
from module.combat.assets import GET_ITEMS_1
from module.exception import GameBugError
from module.guild.assets import *
from module.guild.base import GuildBase
from module.logger import logger
from module.ocr.ocr import Digit
from module.statistics.item import ItemGrid

EXCHANGE_GRIDS = ButtonGrid(
    origin=(470, 470), delta=(198.5, 0), button_shape=(83, 83), grid_shape=(3, 1), name='EXCHANGE_GRIDS')
EXCHANGE_BUTTONS = ButtonGrid(
    origin=(440, 609), delta=(198.5, 0), button_shape=(144, 31), grid_shape=(3, 1), name='EXCHANGE_BUTTONS')
EXCHANGE_FILTER = Filter(regex=re.compile('^(.*?)$'), attr=('name',))
GUILD_SUPPLY_MAX_RETRY = 2
GUILD_EXCHANGE_BUG_RETRY = 5


class ExchangeLimitOcr(Digit):
    """OCR-распознаватель оставшихся попыток обмена гильдии.

    Выполняет инверсию цветов и полутоновое отображение для повышения точности распознавания чисел.
    """
    def pre_process(self, image):
        """
        Args:
            image (np.ndarray): Форма (высота, ширина, каналы)

        Returns:
            np.ndarray: Форма (ширина, высота)
        """
        return 255 - color_mapping(rgb2gray(image), max_multiply=2.5)


GUILD_EXCHANGE_LIMIT = ExchangeLimitOcr(OCR_GUILD_EXCHANGE_LIMIT, threshold=64)


class GuildLogistics(GuildBase):
    """Обработчик снабжения гильдии.

    Отвечает за все автоматизированные операции на странице снабжения гильдии:
    - Получение регулярных припасов (supply)
    - Принятие или сбор наград за еженедельные задания (mission)
    - Обмен ресурсов на предметы (exchange)

    Управляет порядком выполнения подзадач через цикл состояний
    и обрабатывает всплывающие окна и возможные ошибки.

    Attributes:
        _guild_logistics_mission_finished (bool): Завершены ли задания гильдии на этой неделе.
        exchange_items (ItemGrid): Сетка предметов обмена для распознавания доступных типов предметов.
    """
    _guild_logistics_mission_finished = False

    @cached_property
    def exchange_items(self):
        """Получение экземпляра сетки предметов обмена.

        Загружает шаблоны из `./assets/stats_basic` для распознавания типов обмениваемых предметов.

        Returns:
            ItemGrid: Объект сетки предметов обмена.
        """
        item_grid = ItemGrid(
            EXCHANGE_GRIDS, {}, template_area=(40, 21, 89, 70), amount_area=(60, 71, 91, 92))
        item_grid.load_template_folder('./assets/stats_basic')
        return item_grid

    def _is_in_guild_logistics(self):
        """
        Цветовая выборка GUILD_LOGISTICS_ENSURE_CHECK
        для определения, отображается ли он
        в данный момент

        Pages:
            in: GUILD_LOGISTICS
            out: GUILD_LOGISTICS
        """
        # Цвета Axis (181, 97, 99) и Azur (148, 178, 255)
        return bool(
            self.image_color_count(
                GUILD_LOGISTICS_ENSURE_CHECK,
                color=(181, 97, 99),
                threshold=221,
                count=400,
            )
            or self.image_color_count(
                GUILD_LOGISTICS_ENSURE_CHECK,
                color=(148, 178, 255),
                threshold=221,
                count=400,
            )
        )

    def _guild_logistics_ensure(self, skip_first_screenshot=True):
        """
        Ожидание загрузки логистики гильдии
        После входа в логистику гильдии сначала загружается фон, затем St.Louis / Leipzig, затем логистика гильдии

        Args:
            skip_first_screenshot (bool):
        """
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self._is_in_guild_logistics():
                break

    @Config.when(SERVER='en')
    def _guild_logistics_mission_available(self):
        """
        Цветовая выборка области GUILD_MISSION для определения,
        активна ли кнопка, задание уже
        выполняется или новые задания принять нельзя

        Используется как минимум дважды: «Collect» и «Accept»

        Returns:
            bool: Активна ли кнопка

        Pages:
            in: GUILD_LOGISTICS
            out: GUILD_LOGISTICS
        """
        r, g, b = get_color(self.device.image, GUILD_MISSION.area)
        if g > max(r, b) - 10:
            # Зелёная галочка в правом нижнем углу, если задание гильдии завершено
            logger.info('[Гильдия — логистика] Задание гильдии на эту неделю завершено')
            self._guild_logistics_mission_finished = True
            return False
        # На EN «0/300» выделено жирным и чисто белое, а «Collect rewards» —
        # синевато-белое, поэтому условие if инвертировано
        elif self.image_color_count(GUILD_MISSION, color=(255, 255, 255), threshold=235, count=100):

            logger.info('[Гильдия — логистика] Кнопка задания гильдии неактивна')
            return False
        elif self.image_color_count(GUILD_MISSION, color=(255, 255, 255), threshold=180, count=50):
            # белых пикселей меньше 50, но есть синевато-белые пиксели
            logger.info('[Гильдия — логистика] Кнопка задания гильдии активна')
            return True
        else:
            # Счётчик задания гильдии отсутствует
            logger.info('[Гильдия — логистика] Задание гильдии не найдено; возможно, задание этой недели ещё не началось')
            return False
            # if self.image_color_count(GUILD_MISSION_CHOOSE, color=(255, 255, 255), threshold=221, count=100):
            #     # Выбор задания гильдии доступен, если пользователь — мастер гильдии
            #     logger.info('Guild mission choose found')
            #     return True
            # else:
            #     logger.info('Guild mission choose not found')
            #     return False

    @Config.when(SERVER='jp')
    def _guild_logistics_mission_available(self):
        """
        Цветовая выборка области GUILD_MISSION для определения,
        активна ли кнопка, задание уже
        выполняется или новые задания принять нельзя

        Используется как минимум дважды: «Collect» и «Accept»

        Returns:
            bool: Активна ли кнопка

        Pages:
            in: GUILD_LOGISTICS
            out: GUILD_LOGISTICS
        """
        r, g, b = get_color(self.device.image, GUILD_MISSION.area)
        if g > max(r, b) - 10:
            # Зелёная галочка в правом нижнем углу, если задание гильдии завершено
            logger.info('[Гильдия — логистика] Задание гильдии на эту неделю завершено')
            self._guild_logistics_mission_finished = True
            return False
        elif self.image_color_count(GUILD_MISSION, color=(255, 255, 255), threshold=254, count=50):
            # 0/300 на JP имеет цвет (255, 255, 255)
            logger.info('[Гильдия — логистика] Кнопка задания гильдии неактивна')
            return False
        elif self.image_color_count(GUILD_MISSION, color=(255, 255, 255), threshold=180, count=400):
            # (255, 255, 255) меньше 50, но есть много синевато-белых пикселей
            logger.info('[Гильдия — логистика] Кнопка задания гильдии активна')
            return True
        elif not self.image_color_count(GUILD_MISSION, color=(255, 255, 255), threshold=180, count=50):
            # Счётчик задания гильдии отсутствует
            logger.info('[Гильдия — логистика] Задание гильдии не найдено; возможно, задание этой недели ещё не началось')
            # Выбор задания гильдии на сервере JP отключён, пока не получим снимок экрана.
            return False
            # if self.image_color_count(GUILD_MISSION_CHOOSE, color=(255, 255, 255), threshold=221, count=100):
            #     # Выбор задания гильдии доступен, если пользователь — мастер гильдии
            #     logger.info('Guild mission choose found')
            #     return True
            # else:
            #     logger.info('Guild mission choose not found')
            #     return False
        else:
            logger.info('[Гильдия — логистика] Неизвестное состояние задания гильдии; пропуск')
            return False

    @Config.when(SERVER=None)
    def _guild_logistics_mission_available(self):
        """
        Цветовая выборка области GUILD_MISSION для определения,
        активна ли кнопка, задание уже
        выполняется или новые задания принять нельзя

        Используется как минимум дважды: «Collect» и «Accept»

        Returns:
            bool: Активна ли кнопка

        Pages:
            in: GUILD_LOGISTICS
            out: GUILD_LOGISTICS
        """
        r, g, b = get_color(self.device.image, GUILD_MISSION.area)
        if g > max(r, b) - 10:
            # Зелёная галочка в правом нижнем углу, если задание гильдии завершено
            logger.info('[Гильдия — логистика] Задание гильдии на эту неделю завершено')
            self._guild_logistics_mission_finished = True
            return False
        elif self.image_color_count(GUILD_MISSION, color=(255, 255, 255), threshold=180, count=400):
            # Диапазон приёма/сбора незавершённого задания примерно от 240 до 322
            logger.info('[Гильдия — логистика] Кнопка задания гильдии активна')
            return True
        elif not self.image_color_count(GUILD_MISSION, color=(255, 255, 255), threshold=180, count=50):
            # Счётчик задания гильдии отсутствует
            logger.info('[Гильдия — логистика] Задание гильдии не найдено; возможно, задание этой недели ещё не началось')
            return False
            # if self.image_color_count(GUILD_MISSION_CHOOSE, color=(255, 255, 255), threshold=221, count=100):
            #     # Выбор задания гильдии доступен, если пользователь — мастер гильдии
            #     logger.info('Guild mission choose found')
            #     return True
            # else:
            #     logger.info('Guild mission choose not found')
            #     return False
        else:
            logger.info('[Гильдия — логистика] Кнопка задания гильдии неактивна')
            return False

    def _guild_logistics_supply_available(self):
        """
        Цветовая выборка области GUILD_SUPPLY для определения,
        активна кнопка или отключена

        режим определяет

        Returns:
            bool: Активна ли кнопка

        Pages:
            in: GUILD_LOGISTICS
            out: GUILD_LOGISTICS
        """
        color = get_color(self.device.image, GUILD_SUPPLY.area)
        # У активной кнопки белые буквы, у неактивной — серые
        if np.max(color) > np.mean(color) + 25:
            # Для участников — клик для получения снабжения
            # Для лидеров — клик для покупки и получения снабжения
            logger.debug('[Гильдия — логистика] Кнопка снабжения гильдии активна')
            return True
        else:
            logger.debug('[Гильдия — логистика] Кнопка снабжения гильдии неактивна')
            return False

    def _handle_guild_fleet_mission_start(self):
        """
        Выбор нового еженедельного задания флота.
        Текущий аккаунт должен быть мастером или офицером гильдии.

        Returns:
            bool: Был ли выполнен клик
        """
        if not self.config.GuildLogistics_SelectNewMission:
            return False

        if self.appear_then_click(GUILD_MISSION_NEW, offset=(20, 20), interval=2):
            return True
        return bool(
            self.appear_then_click(
                GUILD_MISSION_SELECT, offset=(20, 20), interval=2
            )
        )

    def _guild_logistics_supply_check_finished(self, state):
        """
        Отметить снабжение гильдии как проверенное и сбросить ожидающее состояние клика.

        Args:
            state (dict): Состояние проверки снабжения.
        """
        state['checked'] = True
        state['clicked'] = False

    def _guild_logistics_supply_handle(self, state, click_interval, result_timer):
        """
        Обработка цикла повторных попыток получения снабжения гильдии.

        Args:
            state (dict): Состояние проверки снабжения.
            click_interval (Timer): Управление интервалом кликов.
            result_timer (Timer): Управление ожиданием результата.

        Returns:
            bool: Обработано ли действие и должен ли цикл продолжаться.
        """
        if state['checked']:
            return False

        if not state['clicked']:
            if not self._guild_logistics_supply_available():
                return False

            if click_interval.reached():
                self.device.click(GUILD_SUPPLY)
                click_interval.reset()
                state['clicked'] = True
                state['click_count'] += 1
                result_timer.reset()
            return True

        if not self._guild_logistics_supply_available():
            self._guild_logistics_supply_check_finished(state)
            return False

        if not result_timer.reached():
            return True

        if state['click_count'] >= GUILD_SUPPLY_MAX_RETRY:
            logger.warning('[Гильдия — логистика] После повторных попыток снабжение гильдии всё ещё доступно; пропуск в этом запуске')
            self._guild_logistics_supply_check_finished(state)
            return False

        if click_interval.reached():
            self.device.click(GUILD_SUPPLY)
            click_interval.reset()
            state['click_count'] += 1
            result_timer.reset()
        return True

    def _guild_logistics_exchange_bug_check(self, exchange_count):
        """
        Проверка игровой ошибки обновления после повторных попыток обмена.

        Args:
            exchange_count (int): Число кликов обмена в текущем запуске.
        """
        if exchange_count < GUILD_EXCHANGE_BUG_RETRY:
            return

        # Если запускать Azur Lane несколько дней подряд и затем делать обмен гильдии,
        # появится ошибка о том, что время ещё не пришло.
        # Перезапуск игры проблему не исправляет.
        # Чтобы исправить, нужно один раз войти в логистику гильдии, затем перезапустить.
        # Если обмен выполняется 5 раз, считается, что эта ошибка сработала.
        logger.warning(
            'Не удалось выполнить обмен гильдии; вероятно, таймер в игре работает некорректно')
        raise GameBugError('Обнаружена ошибка обновления логистики гильдии')

    def _guild_logistics_timer_reset(self, confirm_timer, exchange_interval=None):
        """
        Сброс таймеров стабильного состояния логистики и обмена.

        Args:
            confirm_timer (Timer): Таймер стабильного состояния.
            exchange_interval (Timer): Таймер повторных попыток обмена.
        """
        confirm_timer.reset()
        if exchange_interval is not None:
            exchange_interval.reset()

    def _guild_logistics_popup_handle(self, supply_state, confirm_timer, exchange_interval):
        """
        Обработка всплывающих окон логистики и экранов получения наград.

        Args:
            supply_state (dict): Состояние проверки снабжения.
            confirm_timer (Timer): Таймер стабильного состояния.
            exchange_interval (Timer): Таймер повторных попыток обмена.

        Returns:
            bool: Обработано ли действие и должен ли цикл продолжаться.
        """
        if self.handle_popup_confirm('GUILD_LOGISTICS'):
            self._guild_logistics_timer_reset(confirm_timer, exchange_interval)
            return True

        if self.appear_then_click(GET_ITEMS_1, interval=2):
            if supply_state['clicked']:
                self._guild_logistics_supply_check_finished(supply_state)
            self._guild_logistics_timer_reset(confirm_timer, exchange_interval)
            return True

        if self._handle_guild_fleet_mission_start():
            self._guild_logistics_timer_reset(confirm_timer)
            return True

        return False

    def _guild_logistics_mission_handle(self, mission_checked, click_interval):
        """
        Обработка действия сбора или принятия задания гильдии.

        Args:
            mission_checked (bool): Проверено ли задание.
            click_interval (Timer): Управление интервалом кликов.

        Returns:
            tuple[bool, bool]: Новое состояние проверки и должен ли цикл продолжаться.
        """
        if mission_checked:
            return True, False

        if not self._guild_logistics_mission_available():
            return True, False

        if click_interval.reached():
            self.device.click(GUILD_MISSION)
            click_interval.reset()
        return False, True

    def _guild_logistics_exchange_handle(self, exchange_checked, exchange_count, exchange_interval):
        """
        Обработка действия обмена гильдии.

        Args:
            exchange_checked (bool): Проверен ли обмен.
            exchange_count (int): Число кликов обмена в текущем запуске.
            exchange_interval (Timer): Таймер повторных попыток обмена.

        Returns:
            tuple[bool, int, bool]: Новое состояние проверки, число обменов и должен ли цикл продолжаться.
        """
        if exchange_checked or not exchange_interval.reached():
            return exchange_checked, exchange_count, False

        if not self._guild_exchange():
            return True, exchange_count, False

        exchange_interval.reset()
        return False, exchange_count + 1, True

    def _guild_logistics_collect(self, skip_first_screenshot=True):
        """
        Выполнение переходов экрана сбора/принятия внутри
        логистики

        Args:
            skip_first_screenshot (bool):

        Returns:
            bool: Проверена ли вся логистика гильдии; повторная проверка сегодня не нужна.

        Pages:
            in: GUILD_LOGISTICS
            out: GUILD_LOGISTICS
        """
        logger.hr('Логистика гильдии')
        logger.attr('Выбор нового задания гильдии', self.config.GuildLogistics_SelectNewMission)
        confirm_timer = Timer(1.5, count=3).start()
        exchange_interval = Timer(1.5, count=3)
        click_interval = Timer(0.5, count=1)
        supply_state = {
            'checked': False,
            'clicked': False,
            'click_count': 0,
        }
        supply_result_timer = Timer(1.5, count=3)
        mission_checked = False
        exchange_checked = False
        exchange_count = 0

        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self._guild_logistics_popup_handle(supply_state, confirm_timer, exchange_interval):
                continue

            if self._is_in_guild_logistics():
                if self._guild_logistics_supply_handle(supply_state, click_interval, supply_result_timer):
                    self._guild_logistics_timer_reset(confirm_timer)
                    continue
                mission_checked, handled = self._guild_logistics_mission_handle(mission_checked, click_interval)
                if handled:
                    self._guild_logistics_timer_reset(confirm_timer)
                    continue
                exchange_checked, exchange_count, handled = self._guild_logistics_exchange_handle(
                    exchange_checked, exchange_count, exchange_interval)
                if handled:
                    self._guild_logistics_timer_reset(confirm_timer)
                    continue
                if not self.info_bar_count() and confirm_timer.reached():
                    break
                self._guild_logistics_exchange_bug_check(exchange_count)

            else:
                confirm_timer.reset()

        logger.debug(
            "Состояние логистики гильдии: снабжение_проверено=%s, "
            "задание_проверено=%s, обмен_проверен=%s, задание_завершено=%s",
            supply_state["checked"],
            mission_checked,
            exchange_checked,
            self._guild_logistics_mission_finished,
        )
        # Azur Lane теперь выдаёт новые задания гильдии
        # Больше не считаем `self._guild_logistics_mission_finished` условием проверки
        return all([supply_state['checked'], mission_checked, exchange_checked])

    def _guild_exchange_scan(self):
        """
        Сканирование изображения доступных вариантов.
        Предметы, недоступные для обмена, помечаются enough=False.

        Returns:
            list[Item]:

        Pages:
            in: GUILD_LOGISTICS
            out: GUILD_LOGISTICS
        """
        # Сканируем доступные для выбора предметы обмена
        items = self.exchange_items.predict(self.device.image, name=True, amount=False)

        # Перебираем EXCHANGE_GRIDS в поиске красного текста в правом нижнем углу,
        # означающего нехватку этого предмета в инвентаре игрока
        for item, button in zip(items, EXCHANGE_GRIDS.buttons):
            area = area_offset((35, 64, 83, 83), button.area[:2])
            item.enough = not self.image_color_count(area, color=(255, 93, 90), threshold=221, count=20)

        text = [str(item.name) if item.enough else f'{item.name} (not enough)' for item in items]
        logger.info(f'[Гильдия — логистика] Предметы обмена: {", ".join(text)}')
        return items

    def _guild_exchange(self):
        """
        Выполняет проверку по фильтру и выполняет подходящие
        обмены, их число ограничено лимитом
        Если обмен вообще невозможен, цикл завершается
        досрочно

        Returns:
            bool: Был ли выполнен клик.

        Pages:
            in: GUILD_LOGISTICS
            out: GUILD_LOGISTICS
        """
        if GUILD_EXCHANGE_LIMIT.ocr(self.device.image) <= 0:
            return False

        items = self._guild_exchange_scan()
        EXCHANGE_FILTER.load(self.config.GuildLogistics_ExchangeFilter)
        selected = EXCHANGE_FILTER.apply(items, func=lambda item: item.enough)
        logger.attr('Порядок обмена', ' > '.join([str(item.name) for item in selected]))

        if len(selected):
            button = EXCHANGE_BUTTONS.buttons[items.index(selected[0])]
            # Просто клик без разбора, повторная попытка в self._guild_logistics_collect
            self.device.click(button)
            return True
        else:
            logger.warning('[Гильдия — логистика] Нет предметов обмена, соответствующих текущему фильтру, либо недостаточно ресурсов')
            return False

    def guild_logistics(self):
        """
        Выполнение всех действий в логистике

        Returns:
            bool: Проверена ли вся логистика гильдии; повторная проверка сегодня не нужна.

        Pages:
            in: page_guild
            out: page_guild, GUILD_LOGISTICS
        """
        logger.hr('Логистика гильдии', level=1)
        self.guild_side_navbar_ensure(bottom=3)
        self._guild_logistics_ensure()

        result = self._guild_logistics_collect()
        logger.info(f'[Гильдия — логистика] Логистика выполнена успешно: {result}')
        return result
