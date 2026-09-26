"""Обработчик операций гильдии, управляющий входом в операцию, отправкой флотов и контролем прогресса.

Использует OCR для считывания прогресса операции, поддерживает автоматический выбор новой операции.
"""

from module.base.button import ButtonGrid
from module.base.timer import Timer
from module.base.utils import *
from module.config.utils import get_server_monthday
from module.exception import GameBugError
from module.guild.assets import *
from module.guild.base import GuildBase
from module.logger import logger
from module.ocr.ocr import DigitCounter
from module.template.assets import TEMPLATE_OPERATIONS_RED_DOT

GUILD_OPERATIONS_PROGRESS = DigitCounter(OCR_GUILD_OPERATIONS_PROGRESS, letter=(255, 247, 247), threshold=64)


class GuildOperations(GuildBase):
    def _guild_operations_ensure(self, skip_first_screenshot=True):
        """
        Проверка полной загрузки операции гильдии.

        После входа в операцию гильдии сначала загружается фон, затем отображается интерфейс отправки/босса.

        Returns:
            bool: True при успешном входе в операцию, False при нехватке средств гильдии.
        """
        logger.attr('Выбор новой операции гильдии', self.config.GuildOperation_SelectNewOperation)
        confirm_timer = Timer(1.5, count=3).start()
        click_count = 0
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            # Завершение
            if click_count > 5:
                # В информационной строке отображается `none4302`.
                # Возможно, операция гильдии уже была запущена другим офицером.
                # Повторный вход на страницу гильдии должен исправить эту проблему.
                logger.warning(
                    '[Гильдия — операция] Не удалось запустить/присоединиться к операции гильдии; '
                    'возможно, операция уже была открыта другим офицером')
                raise GameBugError('[Гильдия — операция] Не удалось запустить/присоединиться к операции гильдии')

            if self._guild_operation_fund_insufficient():
                return False
            if self._handle_guild_operations_start():
                confirm_timer.reset()
                continue
            if self.appear(GUILD_OPERATIONS_JOIN, interval=3):
                if self.image_color_count(GUILD_OPERATIONS_MONTHLY_COUNT, color=(255, 93, 90), threshold=221, count=20):
                    logger.info('[Гильдия — операция] Невозможно присоединиться: месячный лимит попыток исчерпан')
                    self.device.click(GUILD_OPERATIONS_CLICK_SAFE_AREA)
                else:
                    current, remain, total = GUILD_OPERATIONS_PROGRESS.ocr(self.device.image)
                    threshold = total * self.config.GuildOperation_JoinThreshold
                    if current <= threshold:
                        logger.info('[Гильдия — операция] Присоединение к операции: текущий прогресс ниже '
                                    f'порога ({threshold:.2f})')
                        self.device.click(GUILD_OPERATIONS_JOIN)
                    else:
                        logger.info('[Гильдия — операция] Не присоединяемся к операции: текущий прогресс выше '
                                    f'порога ({threshold:.2f})')
                        self.device.click(GUILD_OPERATIONS_CLICK_SAFE_AREA)
                confirm_timer.reset()
                continue
            if self.handle_popup_confirm('JOIN_OPERATION'):
                click_count += 1
                confirm_timer.reset()
                continue
            if self.handle_popup_single('FLEET_UPDATED'):
                logger.info('[Гильдия — операция] Состав флота изменён, но отправка, возможно, всё ещё доступна. '
                            'Участники гильдии обновили свои флоты поддержки. '
                            'Рекомендуется включить рекомендацию для босса')
                confirm_timer.reset()
                continue

            # End
            if self.appear(GUILD_BOSS_ENTER) or self.appear(GUILD_OPERATIONS_ACTIVE_CHECK, offset=(20, 20)):
                if not self.info_bar_count() and confirm_timer.reached():
                    return True

    def _handle_guild_operations_start(self):
        """
        Запуск новой операции гильдии.

        Текущий аккаунт должен быть лидером или офицером гильдии. Не рекомендуется запускать третью операцию за месяц:
        участники могут участвовать только в 2 операциях в месяц, поэтому большинство не сможет отправить флоты,
        что ухудшит оценку событий отправки и уменьшит итоговые награды.

        Returns:
            bool: Была ли нажата кнопка.
        """
        if not self.config.GuildOperation_SelectNewOperation:
            return False

        today = get_server_monthday()
        limit = self.config.GuildOperation_NewOperationMaxDate
        if today >= limit:
            logger.info(f'[Гильдия — операция] Текущая дата {today} >= лимита {limit}; новая операция не запускается')
            return False

        # Жёстко выбираем операцию с наибольшей наградой: воздушно-морское сражение у Соломоновых островов
        if self.appear_then_click(GUILD_OPERATIONS_SOLOMON, offset=(20, 20), interval=3):
            return True
        # Переходим к только что запущенной новой операции
        # Пример последовательности страниц:
        # - GUILD_OPERATIONS_SOLOMON
        # - GUILD_OPERATIONS_NEW
        # - handle_popup_confirm(), подтверждение расходования средств гильдии
        # - GUILD_OPERATIONS_JOIN
        # - GUILD_OPERATIONS_ACTIVE_CHECK
        if self.appear_then_click(GUILD_OPERATIONS_NEW, offset=(20, 20), interval=3):
            return True

        return False

    def _guild_operation_fund_insufficient(self):
        """
        Проверка нехватки средств гильдии.

        Returns:
            bool: True, если средств недостаточно.

        Pages:
            in: GUILD_OPERATIONS_NEW
        """
        if not self.appear(GUILD_OPERATIONS_NEW, offset=(20, 20)):
            return False
        if self.image_color_count(GUILD_OPERATION_FUND_CHECK, color=(255, 93, 91), threshold=180, count=30):
            logger.warning('[Гильдия — операция] Недостаточно средств гильдии для запуска новой операции')
            return True
        return False

    def _guild_operations_get_mode(self):
        """
        Определение типа загруженного меню операций.

        Returns:
            int: Текущий режим операции.
                0 — нет активных операций, лидер/офицер/элита должны выбрать сложность для старта
                1 — операция активна, отображается схема прогресса / сеть задач
                2 — активирован рейд на босса гильдии
                None — меню не удалось определить или подтвердить

        Pages:
            in: GUILD_OPERATIONS
            out: GUILD_OPERATIONS
        """
        if self.appear(GUILD_OPERATIONS_INACTIVE_CHECK) and self.appear(GUILD_OPERATIONS_ACTIVE_CHECK):
            logger.info(
                'Режим: операции неактивны. Попросите элитного участника, офицера или командира '
                'выбрать сложность операции')
            return 0
        elif self.appear(GUILD_OPERATIONS_ACTIVE_CHECK):
            logger.info('[Гильдия — операция] Режим: операция активна, доступны сканирование и отправка флота')
            return 1
        elif self.appear(GUILD_BOSS_ENTER):
            logger.info('[Гильдия — операция] Режим: рейд на босса гильдии (GUILD_BOSS_ENTER)')
            return 2
        elif self.appear(GUILD_OPERATIONS_NEW, offset=(20, 20)):
            logger.info('[Гильдия — операция] Режим: рейд на босса гильдии (GUILD_OPERATIONS_NEW)')
            return 2
        else:
            logger.warning('[Гильдия — операция] Интерфейс операции не распознан')
            return None

    def _guild_operations_get_entrance(self):
        """
        Получение 2 кнопок входа в отправку гильдии.

        Если операция находится вверху, при нажатии кнопки раскрытия цепочка задач сдвигается вниз,
        и кнопка входа появляется вверху, поэтому обе кнопки отслеживаются динамически.

        Returns:
            list[Button], list[Button]: Список кнопок раскрытия, список кнопок входа.

        Pages:
            in: page_guild, guild operation, operation map (GUILD_OPERATIONS_ACTIVE_CHECK)
        """
        # Область всей цепочки задач операции
        detection_area = (152, 135, 1280, 630)
        # Смещаем внутрь, чтобы не нажимать по краям
        pad = 5

        list_expand = []
        list_enter = []
        dots = TEMPLATE_OPERATIONS_RED_DOT.match_multi(self.image_crop(detection_area, copy=False), threshold=5)
        logger.info(f'[Гильдия — операция] Найдено активных операций: {len(dots)}')
        for button in dots:
            button = button.move(vector=detection_area[:2])
            expand = button.crop(area=(-257, 14, 12, 51), name='DISPATCH_ENTRANCE_1')
            enter = button.crop(area=(-257, -109, 12, -1), name='DISPATCH_ENTRANCE_2')
            for b in [expand, enter]:
                b.area = area_limit(b.area, detection_area)
                b._button = area_pad(b.area, pad)
            list_expand.append(expand)
            list_enter.append(enter)

        return list_expand, list_enter

    def _guild_operations_dispatch_swipe(self, forward=True, skip_first_screenshot=True):
        """
        Прокрутка карты в поисках активной задачи отправки.

        Хотя Azur Lane автоматически фокусируется на активной отправке, из-за бага игры фокус
        не всегда доходит до дальних задач, поэтому требуется ручная прокрутка. Принудительно
        используется minitouch, так как uiautomator2 требует большей дистанции жеста.

        Args:
            forward (bool): Направление горизонтального свайпа.
            skip_first_screenshot (bool): Пропускать ли первый снимок экрана.

        Returns:
            bool: Найдена ли активная отправка.
        """
        # Область всей цепочки задач операции
        detection_area = (152, 135, 1280, 630)
        direction_vector = (-600, 0) if forward else (600, 0)

        for _ in range(5):
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            entrance_1, entrance_2 = self._guild_operations_get_entrance()
            if len(entrance_1):
                return True

            p1, p2 = random_rectangle_vector(
                direction_vector, box=detection_area, random_range=(-50, -50, 50, 50), padding=20)
            self.device.drag(p1, p2, segments=2, shake=(0, 25), point_random=(0, 0, 0, 0), shake_random=(0, -5, 0, 5))
            # self.device.sleep(0.3)

        logger.warning('[Гильдия — операция] Активная отправка операции не найдена')
        return False

    def _guild_operations_dispatch_enter(self, skip_first_screenshot=True):
        """
        Вход в интерфейс подготовки отправки флота.

        Returns:
            bool: Успешен ли вход.

        Pages:
            in: page_guild, guild operation, operation map (GUILD_OPERATIONS_ACTIVE_CHECK)
                После входа в операцию гильдии игра автоматически фокусируется на активной задаче;
                фокусировка происходит на основной ветке цепочки, боковые задачи могут игнорироваться.
            out: page_guild, guild operation, operation dispatch preparation (GUILD_DISPATCH_RECOMMEND)
        """
        timer_1 = Timer(2, count=5)
        timer_2 = Timer(2, count=5)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear(GUILD_OPERATIONS_ACTIVE_CHECK, offset=(20, 20)):
                entrance_1, entrance_2 = self._guild_operations_get_entrance()
                if not len(entrance_1):
                    return False
                if timer_1.reached():
                    self.device.click(entrance_1[0])
                    timer_1.reset()
                    continue
                if timer_2.reached():
                    for button in entrance_2:
                        # У кнопки входа справа вверху есть чёрная область вокруг Easy/Normal/Hard
                        # Если операция не раскрыта, кнопка входа — это фон с Gaussian Blur
                        if self.image_color_count(button, color=(0, 0, 0), threshold=235, count=50):
                            self.device.click(button)
                            timer_1.reset()
                            timer_2.reset()
                            break

            if self.appear_then_click(GUILD_DISPATCH_QUICK, offset=(20, 20), interval=2):
                timer_1.reset()
                timer_2.reset()
                continue

            # End
            if self.appear(GUILD_DISPATCH_RECOMMEND, offset=(20, 20)):
                break

        return True

    def _guild_operations_get_dispatch(self):
        """
        Получение кнопки переключения доступного для отправки флота.

        В ранних версиях отслеживалась красная точка на кнопке переключения, но по неизвестным причинам
        она иногда не отображается, поэтому распознаётся сама кнопка переключения.

        Returns:
            Button: Кнопка переключения флота, либо None, если уже выбран крайний правый флот.

        Pages:
            in: page_guild, guild operation, operation dispatch preparation (GUILD_DISPATCH_RECOMMEND)
        """
        # Переключение флота: 4 варианта
        #          | 1 |
        #       | 1 | | 2 |
        #    | 1 | | 2 | | 3 |
        # | 1 | | 2 | | 3 | | 4 |
        #   0  1  2  3  4  5  6   кнопки в switch_grid
        switch_grid = ButtonGrid(origin=(573.5, 381), delta=(20.5, 0), button_shape=(11, 24), grid_shape=(7, 1))
        # Цвет переключателя неактивного флота
        color_active = (74, 117, 222)
        # Цвет текущего флота
        color_inactive = (33, 48, 66)

        text = []
        index = 0
        button = None
        for switch in switch_grid.buttons:
            if self.image_color_count(switch, color=color_inactive, threshold=235, count=30):
                index += 1
                text.append(f'| {index} |')
                button = switch
            elif self.image_color_count(switch, color=color_active, threshold=235, count=30):
                index += 1
                text.append(f'[ {index} ]')
                button = switch

        # Пример лога: | 1 | | 2 | [ 3 ]
        text = ' '.join(text)
        logger.attr('Отправляемый флот', text)
        if text.endswith(']'):
            logger.info('[Гильдия — операция] Уже выбран крайний правый флот')
            return None
        else:
            return button

    def _guild_operations_dispatch_switch_fleet(self, skip_first_screenshot=True):
        """
        Переключение на крайний правый флот.

        Pages:
            in: page_guild, guild operation, operation dispatch preparation (GUILD_DISPATCH_RECOMMEND)
            out: page_guild, guild operation, operation dispatch preparation (GUILD_DISPATCH_RECOMMEND)
        """
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            button = self._guild_operations_get_dispatch()
            if button is None:
                break
            elif point_in_area((640, 393), button.area):
                logger.info('[Гильдия — операция] Отправляется первый флот; переключение пропущено')
            else:
                self.device.click(button)
                # Ждём завершения анимации клика, иначе она помешает распознаванию в _guild_operations_get_dispatch()
                self.device.sleep((0.5, 0.6))
                continue

    def _guild_operations_dispatch_execute(self, skip_first_screenshot=True):
        """
        Выполнение последовательности отправки флота.

        Pages:
            in: page_guild, guild operation, operation dispatch preparation (GUILD_DISPATCH_RECOMMEND)
            out: page_guild, guild operation, operation dispatch preparation (GUILD_DISPATCH_RECOMMEND)
        """
        dispatched = False
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear(GUILD_DISPATCH_FLEET_UNFILLED, offset=(20, 20), interval=3):
                # Здесь не используем offset: GUILD_DISPATCH_FLEET_UNFILLED отличается только цветом
                # Используем более длинный interval, поскольку игре требуется несколько секунд для выбора кораблей
                self.device.click(GUILD_DISPATCH_RECOMMEND)
                continue
            if not dispatched and self.appear(GUILD_DISPATCH_FLEET, offset=(20, 20), interval=3):
                # GUILD_DISPATCH_FLEET и GUILD_DISPATCH_FLEET_UNFILLED имеют одинаковые признаки, но разные цвета
                # Дополнительно подтверждаем по синему фону
                if self.image_color_count(GUILD_DISPATCH_FLEET, color=(82, 93, 221), threshold=235, count=500):
                    self.device.click(GUILD_DISPATCH_FLEET)
                else:
                    self.interval_clear(GUILD_DISPATCH_FLEET)
                continue
            if self.handle_popup_confirm('GUILD_DISPATCH'):
                self.interval_clear(GUILD_DISPATCH_FLEET)
                dispatched = True
                continue

            # Завершение
            if self.appear(GUILD_DISPATCH_IN_PROGRESS):
                # При первой отправке отображается GUILD_DISPATCH_IN_PROGRESS
                logger.info('[Гильдия — операция] Флот отправлен; отправка выполняется')
                break
            if dispatched and self.appear(GUILD_DISPATCH_FLEET, offset=(20, 20), interval=3):
                # GUILD_DISPATCH_FLEET и GUILD_DISPATCH_FLEET_UNFILLED имеют одинаковые признаки, но разные цвета
                # Дополнительно подтверждаем по синему фону
                if self.image_color_count(GUILD_DISPATCH_FLEET, color=(82, 93, 221), threshold=235, count=500):
                    # При последующих отправках отображается GUILD_DISPATCH_FLEET
                    # Невозможно подтвердить, был ли флот уже отправлен,
                    # поскольку GUILD_DISPATCH_FLEET отображается и после рекомендации, но до отправки
                    # _guild_operations_dispatch() повторит попытку, если отправка не состоялась
                    logger.info('[Гильдия — операция] Флот отправлен')
                    break

    def _guild_operations_dispatch_exit(self, skip_first_screenshot=True):
        """
        Выход на карту операции.

        Pages:
            in: page_guild, guild operation, operation dispatch preparation (GUILD_DISPATCH_RECOMMEND)
            out: page_guild, guild operation, operation map (GUILD_OPERATIONS_ACTIVE_CHECK)
        """
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear(GUILD_DISPATCH_RECOMMEND, offset=(20, 20), interval=2):
                self.device.click(GUILD_DISPATCH_CLOSE)
                continue
            if self.appear(GUILD_DISPATCH_QUICK, offset=(20, 20), interval=2):
                self.device.click(GUILD_DISPATCH_CLOSE)
                continue
            if self.appear(GUILD_DISPATCH_IN_PROGRESS, interval=2):
                # Здесь не используем offset: GUILD_DISPATCH_IN_PROGRESS — цветная кнопка
                self.device.click(GUILD_DISPATCH_CLOSE)
                continue

            # Завершение
            if self.appear(GUILD_OPERATIONS_ACTIVE_CHECK):
                break

    def _guild_operations_dispatch(self):
        """
        Выполнение отправки флотов гильдии.

        Pages:
            in: page_guild, guild operation, operation map (GUILD_OPERATIONS_ACTIVE_CHECK)
            out: page_guild, guild operation, operation map (GUILD_OPERATIONS_ACTIVE_CHECK)
        """
        logger.hr('Отправка гильдии')
        success = False
        for _ in reversed(range(2)):
            if self._guild_operations_dispatch_swipe(forward=_):
                success = True
                break
            if _:
                self.guild_side_navbar_ensure(bottom=2)
                self.guild_side_navbar_ensure(bottom=1)
                self._guild_operations_ensure()
        if not success:
            return False

        for _ in range(5):
            if self._guild_operations_dispatch_enter():
                self._guild_operations_dispatch_switch_fleet()
                self._guild_operations_dispatch_execute()
                self._guild_operations_dispatch_exit()
            else:
                return True

        logger.warning('[Гильдия — операция] Слишком много попыток отправки в операцию гильдии')
        return False

    def _guild_operations_boss_preparation(self, az, skip_first_screenshot=True):
        """
        Последовательность подготовки к рейду на босса гильдии.

        az — экземпляр GuildCombat, используемый для обработки боевых интерфейсов.
        Создаётся отдельно во избежание конфликтов и переопределений методов базовых классов.

        Pages:
            in: GUILD_OPERATIONS_BOSS
            out: IN_BATTLE
        """
        is_loading = False
        dispatch_count = 0
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear_then_click(GUILD_BOSS_ENTER, interval=3):
                continue

            if self.appear(GUILD_DISPATCH_FLEET, offset=(20, 20), interval=3):
                # Кнопка не становится серой, даже если состав флота пуст
                if dispatch_count < 5:
                    self.device.click(GUILD_DISPATCH_FLEET)
                    dispatch_count += 1
                else:
                    logger.warning('[Гильдия — операция] Ошибка состава флота. Предварительно загруженный выбор поддержки гильдии '
                                   'может блокировать отправку. Рекомендуется включить рекомендацию для босса')
                    return False
                continue

            if self.config.GuildOperation_BossFleetRecommend:
                if self.info_bar_count() and self.appear_then_click(GUILD_DISPATCH_RECOMMEND_2, interval=3):
                    continue

            # Логируем только при первом обнаружении
            if not is_loading:
                if az.is_combat_loading():
                    self.device.screenshot_interval_set('combat')
                    is_loading = True
                    continue

            if az.handle_combat_automation_confirm():
                continue

            # Завершение
            pause = az.is_combat_executing()
            if pause:
                logger.attr('Боевой интерфейс', pause)
                return True

    def _guild_operations_boss_combat(self):
        """
        Боевая последовательность сражения с боссом. Если подготовка не удалась, завершает выполнение.

        Pages:
            in: GUILD_OPERATIONS_BOSS
            out: GUILD_OPERATIONS_BOSS
        """
        from module.guild.guild_combat import GuildCombat
        az = GuildCombat(self.config, device=self.device)

        if not self._guild_operations_boss_preparation(az):
            return False
        az.combat_execute(auto='combat_auto', submarine='every_combat')
        az.combat_status(expected_end='in_ui')
        logger.info('[Гильдия — операция] Рейдовый босс гильдии побеждён')
        return True

    def _guild_operations_boss_available(self):
        """
        Проверка доступности босса гильдии.

        Returns:
            bool: Доступен ли босс.
        """
        appear = self.image_color_count(GUILD_BOSS_AVAILABLE, color=(140, 243, 99), threshold=221, count=10)
        if appear:
            logger.info('[Гильдия — операция] Босс гильдии доступен')
        else:
            logger.info('[Гильдия — операция] Босс гильдии недоступен')
        return appear

    def guild_operations(self):
        logger.hr('Операция гильдии', level=1)
        self.guild_side_navbar_ensure(bottom=1)
        entered = self._guild_operations_ensure()
        if not entered:
            logger.info(f'[Гильдия — операция] Выполнение операции гильдии успешно: {entered}')
            return False
        # Определяем режим операции; сейчас их три
        operations_mode = self._guild_operations_get_mode()

        # Выполняем действие в соответствии с обнаруженным режимом
        result = True
        if operations_mode == 0:
            pass
        elif operations_mode == 1:
            self._guild_operations_dispatch()
        elif operations_mode == 2:
            if self._guild_operations_boss_available():
                if self.config.GuildOperation_AttackBoss:
                    result = self._guild_operations_boss_combat()
                else:
                    logger.info('[Гильдия — операция] Автоматический бой отключён; это задание гильдии необходимо завершить вручную')
        else:
            result = False

        logger.info(f'[Гильдия — операция] Выполнение операции гильдии успешно: {result}')
        return result
