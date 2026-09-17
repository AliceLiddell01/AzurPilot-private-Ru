"""Модуль управления боями META с маяками Пепла.

Обрабатывает сражения META в системе маяков Пепла (Ash / META) Operation Siren,
включая OCR уровня маяка, распознавание нанесённого урона, отслеживание и навигацию по экранам статуса META,
получение наград, а также автоматический поиск доступных маяков и запуск испытаний.
"""
import re
from enum import Enum

import module.config.server as server
from module.base.timer import Timer
from module.combat.combat import BATTLE_PREPARATION
from module.logger import logger
from module.meta_reward.meta_reward import MetaReward
from module.ocr.ocr import Digit, DigitCounter
from module.os_ash.ash import AshCombat
from module.os_ash.assets import *
from module.os_handler.map_event import MapEventHandler
from module.ui.assets import BACK_ARROW
from module.ui.page import page_reward
from module.ui.ui import UI


class MetaState(Enum):
    """Перечисление состояний экрана META."""
    INIT = 'no meta begin'
    ATTACKING = 'a meta under attack'
    COMPLETE = 'reward to be collected'
    UNDEFINED = 'a undefined page'


OCR_BEACON_TIER = Digit(BEACON_TIER, name='OCR_ASH_TIER')
if server.server != 'jp':
    OCR_META_DAMAGE = Digit(META_DAMAGE, name='OCR_META_DAMAGE')
else:
    OCR_META_DAMAGE = Digit(META_DAMAGE, letter=(201, 201, 201), name='OCR_META_DAMAGE')


class MetaDigitCounter(DigitCounter):
    """Числовой счётчик META с коррекцией типичных ошибок OCR."""

    def after_process(self, result):
        """
        Постобработка результатов OCR для исправления типичных ошибок распознавания.

        Логика обработки:
        - "00/200" -> "100/200" (начальный 0 распознан как цифра 0)
        - "23" -> "2/3" (потерян слэш, исправляется, только если первая цифра 0-3)
        - "1/40/1400" -> "140/1400" (лишний слэш)
        """
        result = super().after_process(result)

        # "00/200" -> "100/200"
        if result.startswith('00/'):
            result = '100/' + result[3:]

        # "23" -> "2/3"
        if re.match(r'^[0123]3$', result):
            result = f'{result[0]}/{result[1]}'

        # "1/40/1400" -> "140/1400"
        for suffix in ['/1400', '/200']:
            if result.endswith(suffix):
                point = result[:-len(suffix)]
                point = point.replace('/', '')
                result = point + suffix

        return result


class Meta(UI, MapEventHandler):
    """Базовый модуль боёв META, обрабатывающий события карты и OCR-распознавание."""

    def digit_ocr_point_and_check(self, button: Button, check_number: int):
        """
        Считать число на кнопке через OCR и проверить, достигнут ли порог.

        Args:
            button: Область кнопки для распознавания.
            check_number: Пороговое значение.

        Returns:
            bool: Достигло ли распознанное значение порога (>= check_number).
        """
        point_ocr = MetaDigitCounter(button, letter=(235, 235, 235), threshold=160, name='POINT_OCR')
        point, _, _ = point_ocr.ocr(self.device.image)
        if point >= check_number:
            return True
        return False

    def handle_map_event(self, drop=None):
        """
        Обработать всплывающие окна различных событий на карте META.

        Обрабатывает подтверждение завершения авто-атаки, случайный переход на экран помощи
        или на экран подготовки к бою.

        Args:
            drop: Обработчик изображений дропа.

        Returns:
            bool: Было ли выполнено действие.
        """
        if super().handle_map_event(drop):
            return True
        if self.appear_then_click(META_AUTO_CONFIRM, offset=(20, 20), interval=2):
            logger.info('[META — бой] Обнаружено завершение автоматической атаки')
            return True
        if self.appear(HELP_CONFIRM, offset=(30, 30), interval=2):
            logger.info('[META — бой] Случайно открыт экран подтверждения помощи')
            self.device.click(BACK_ARROW)
            return True
        if self.appear(BATTLE_PREPARATION, offset=(30, 30), interval=2):
            logger.info('[META — бой] Случайно открыт экран подготовки к бою')
            self.device.click(BACK_ARROW)
            return True
        if self.handle_popup_cancel('META'):
            return True
        if self.appear_then_click(META_ENTRANCE, offset=(20, 300), interval=2):
            return True
        return False


def _server_support():
    """Поддерживает ли текущий сервер маяки и OneHitMode."""
    return server.server in ['cn', 'en', 'jp', 'tw']


def _server_support_dossier_auto_attack():
    """Поддерживает ли текущий сервер авто-атаку досье."""
    return server.server in ['cn', 'en']


class OpsiAshBeacon(Meta):
    """Основная задача маяков Пепла: атака META, получение наград и планирование задач."""
    _meta_receive = []
    _meta_category = "undefined"

    def _attack_meta(self, skip_first_screenshot=True):
        """
        Обработать полный цикл атаки META.

        Диспетчеризация по состоянию страницы: при INIT выбирает маяк или досье,
        при ATTACKING проводит бой, при COMPLETE забирает награды.

        Pages:
            in: in_meta
            out: in_meta
        """
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.handle_map_event():
                continue
            state = self._get_state()
            logger.info('[META — бой] Состояние страницы: ' + state.name)
            if MetaState.UNDEFINED == state:
                continue
            if MetaState.INIT == state:
                if self._begin_meta():
                    continue
                else:
                    # Обычное завершение
                    break
            if MetaState.ATTACKING == state:
                # Exit beacon pages when in dossier-only mode
                if self.config.OpsiAshBeacon_AttackMode == 'current_dossier_only' \
                        and self.appear(BEACON_LIST, offset=(20, 20)):
                    self.appear_then_click(ASH_QUIT, offset=(10, 10), interval=2)
                    continue
                if not self._pre_attack():
                    continue
                if self._satisfy_attack_condition():
                    self._make_an_attack()
                    continue
            if MetaState.COMPLETE == state:
                if self.appear(BEACON_LIST, offset=(20, 20)):
                    self._meta_category = "beacon"
                elif self.appear(DOSSIER_LIST, offset=(20, 20)):
                    self._meta_category = "dossier"
                self._handle_ash_beacon_reward()
                if not self._meta_category in self._meta_receive:
                    self._meta_receive.append(self._meta_category)
                # После уничтожения META проверяем, нужно ли переключить другие задачи
                self.config.check_task_switch()
                continue

    def _make_an_attack(self):
        """
        Провести один бой META.

        Во время боя обрабатывает случайные переходы на экраны подготовки или помощи;
        после окончания боя проверяет возврат на экран META.

        Pages:
            in: in_meta, ASH_START
            out: in_meta, ASH_START or BEACON_REWARD
        """
        logger.hr('Бой META', level=2)

        def expected_end():
            # При случайном входе на экран подготовки к бою нажимаем «Назад»
            if self.appear(BATTLE_PREPARATION, offset=(30, 30), interval=2):
                logger.info('[META — бой] Случайно открыт экран подготовки к бою')
                self.device.click(BACK_ARROW)
                return False
            # При случайном входе на экран подтверждения помощи возвращаемся через вход помощи
            if self.appear(HELP_CONFIRM, offset=(30, 30), interval=3):
                logger.info('[META — бой] Случайно открыт экран подтверждения помощи')
                self.device.click(HELP_ENTER)
                return False
            # Вернулись на страницу META — бой завершён
            if self._in_meta_page():
                logger.info('[META — бой] Бой завершён, выполнен возврат на нужную страницу')
                return True

            return False

        # Выполняем бой
        combat = AshCombat(config=self.config, device=self.device)
        combat.combat(expected_end=expected_end, save_get_items=False, emotion_reduce=False)

    def _handle_ash_beacon_reward(self, skip_first_screenshot=True):
        """
        Получить награду за уничтожение META.

        Нажимает кнопку награды до исчезновения интерфейса наград и возврата на экран META.

        Pages:
            in: in_meta, BEACON_REWARD
            out: in_meta
        """
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            # Условие завершения: кнопка награды исчезла и выполнен возврат на страницу META
            if not self.appear(BEACON_REWARD, offset=(30, 30)):
                if self._in_meta_page():
                    break

            # Нажимаем получение награды
            if self.appear_then_click(BEACON_REWARD, offset=(30, 30), interval=2):
                logger.info('[META — бой] Получение награды META')
                continue
            # Обрабатываем случайные события
            if self.handle_map_event():
                continue
            # При случайном возврате на главную страницу возвращаемся через вход наград
            if self.ui_main_appear_then_click(page_reward, interval=2):
                continue
            if self.appear(META_ENTRANCE, offset=(20, 300), interval=2):
                continue

    def _satisfy_attack_condition(self):
        """
        Проверить, удовлетворяет ли текущий босс META условиям атаки.

        В режиме маяка: если включён OneHitMode и урон уже нанесён, атака прекращается.
        В режиме архива: если уже идёт автоатака, ручной бой не проводится.

        Returns:
            bool: Удовлетворены ли условия атаки (всегда возвращает True; при отказе задача останавливается через task_stop).
        """
        if self.appear(BEACON_LIST, offset=(20, 20)):
            # OneHitMode включён и текущей META уже нанесён урон
            if _server_support() and self.config.OpsiAshBeacon_OneHitMode:
                damage = self._get_meta_damage()
                if damage > 0:
                    logger.info(f'[META — бой] Включён режим одного удара, текущей цели META уже нанесено {damage} урона; повторная проверка через 30 минут')
                    self.config.task_delay(minute=30)
                    self.ui_goto_main()
                    self.config.task_stop()
        if self.appear(DOSSIER_LIST, offset=(20, 20)):
            # META уже атакуется автоматически
            if self.appear(META_AUTO_ATTACKING, offset=(20, 20)):
                logger.info('[META — бой] Выполняется автоматическая атака цели META; повторная проверка через 15 минут')
                self.config.task_delay(minute=15)
                self.ui_goto_main()
                self.config.task_stop()
        return True

    def _get_meta_damage(self):
        """
        Получить значение нанесённого текущему боссу META урона.

        Returns:
            int: Число урона, распознанное через OCR.
        """
        self._ensure_meta_inner_page_damage()
        return OCR_META_DAMAGE.ocr(self.device.image)

    def _ensure_meta_inner_page_damage(self, skip_first_screenshot=True):
        """
        Переключить внутреннюю вкладку экрана META на отображение урона.

        Если открыта вкладка сведений, кликает для перехода на вкладку урона.

        Pages:
            in: in_meta, ASH_START
            out: in_meta, META_INNER_PAGE_DAMAGE, ASH_START
        """
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.match_template_color(META_INNER_PAGE_DAMAGE, offset=(20, 20)):
                logger.info('[META — бой] Уже открыт экран урона')
                break
            if self.match_template_color(META_INNER_PAGE_NOT_DAMAGE, offset=(20, 20)):
                logger.info('[META — бой] Открыт экран сведений, переход на экран урона')
                self.appear_then_click(META_INNER_PAGE_NOT_DAMAGE, offset=(20, 20), interval=2)
                continue

    def _pre_attack(self):
        """
        Подготовительные действия перед атакой.

        В режиме маяка: отправляет запрос помощи согласно настройкам.
        В режиме архива: для серверов CN/EN запускает автоатаку, на остальных серверах пока не обрабатывается.

        Returns:
            bool: Готова ли атака к запуску.
        """
        # Страница маяка
        if self.appear(BEACON_LIST, offset=(20, 20)):
            if self.config.OpsiAshBeacon_OneHitMode or self.config.OpsiAshBeacon_RequestAssist:
                if not self._ask_for_help():
                    return False
            return True
        # Страница архива
        if self.appear(DOSSIER_LIST, offset=(20, 20)):
            # Автоатака поддерживается и ещё не запущена
            if _server_support_dossier_auto_attack() and self.config.OpsiAshBeacon_DossierAutoAttackMode \
                    and self.appear(META_AUTO_ATTACK_START, offset=(5, 5)):
                return self._dossier_auto_attack()
            return True
        return False

    def _ask_for_help(self):
        """
        Запросить поддержку у друзей, флота и в общем мировом чате.

        Последовательно нажимает три кнопки запроса помощи и подтверждает.

        Returns:
            bool: Успешно ли отправлен запрос помощи. Возвращает False, если цель META завершилась сразу после запроса.

        Pages:
            in: is_in_meta
            out: is_in_meta
        """
        # Переходим на страницу помощи
        skip_first_screenshot = True
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            # Условие завершения: появился экран подтверждения помощи
            if self.appear(HELP_CONFIRM, offset=(20, 20)):
                break
            # Нажимаем вход в помощь
            if self.appear_then_click(HELP_ENTER, offset=(20, 20), interval=3):
                continue
            # При случайном входе на экран подготовки к бою нажимаем «Назад»
            if self.appear(BATTLE_PREPARATION, offset=(30, 30), interval=2):
                self.device.click(BACK_ARROW)
                continue

        # Последовательно нажимаем три кнопки помощи, не проверяя выбранное состояние
        self.device.click(HELP_3)
        self.device.sleep((0.1, 0.3))
        self.device.click(HELP_2)
        self.device.sleep((0.1, 0.3))
        self.device.click(HELP_1)

        skip_first_screenshot = True
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            # Условие завершения: экран подтверждения помощи исчез
            # Иногда окно помощи отображается без чёрного размытого фона, поэтому HELP_CONFIRM и HELP_ENTER появляются одновременно
            if not self.appear(HELP_CONFIRM, offset=(30, 30)):
                if self.appear(HELP_ENTER, offset=(30, 30)):
                    return True
                # META могла завершиться сразу после запроса помощи
                if self.appear(BEACON_REWARD, offset=(30, 30)):
                    logger.info('[META — поддержка] После запроса помощи цель META была завершена; эта попытка поддержки пропущена')
                    return False
            # Нажимаем подтверждение
            if self.appear_then_click(HELP_CONFIRM, offset=(30, 30), interval=3):
                continue

    def _dossier_auto_attack(self):
        """
        Запустить автоатаку в архиве досье.

        Нажимает кнопку старта автоатаки и подтверждает до появления метки выполнения автоатаки.

        Returns:
            bool: Успешно ли включена автоатака.

        Pages:
            in: is_in_meta & not auto attacking
            out: is_in_meta
        """
        timeout = Timer(10, count=20).start()
        skip_first_screenshot = True
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            # Условие завершения: автоатака запущена
            if self.appear(META_AUTO_ATTACKING, offset=(5, 5)):
                return True
            if timeout.reached():
                logger.warning('[META — бой] Истекло время запуска автоатаки архива: возможно, кнопка запуска не найдена')
                return False
            # Цель уже уничтожена другим игроком
            if self.appear(BEACON_REWARD, offset=(30, 30)):
                return False

            # Нажимаем подтверждение и кнопку запуска автоатаки
            if self.appear_then_click(META_AUTO_ATTACK_CONFIRM, offset=(5, 5), interval=3):
                continue
            if self.appear_then_click(META_AUTO_ATTACK_START, offset=(5, 5), interval=3):
                continue
            # При случайном входе на экран подготовки к бою нажимаем «Назад»
            if self.appear(BATTLE_PREPARATION, offset=(30, 30), interval=2):
                self.device.click(BACK_ARROW)
                continue

    def _begin_meta(self):
        """
        Выбрать или начать бой META вне зависимости от текущего экрана META.

        На главном экране META: переходит к маяку или досье.
        На экране маяка/досье: запускает новый бой META либо возвращается на главный экран.

        Returns:
            bool: Требуется ли продолжить цикл.
        """
        
        attack_mode = self.config.OpsiAshBeacon_AttackMode
        # Главная страница META
        if self.appear(ASH_SHOWDOWN, offset=(30, 30), interval=2):
            # Вход в маяк
            if attack_mode != 'current_dossier_only':
                if self._check_beacon_point():
                    self.device.click(META_MAIN_BEACON_ENTRANCE)
                    logger.info('[META — бой] Выбран вход к маяку')
                    return True
            # Вход в архив

            if _server_support() \
                    and attack_mode != 'current' \
                    and self._check_dossier_point():
                if self.appear_then_click(META_MAIN_DOSSIER_ENTRANCE, offset=(20, 20), interval=2):
                    logger.info('[META — бой] Выбран вход к архиву')
                    return True
                else:
                    logger.info('[META — бой] Архив не выбран')
            return False
        # Страница маяка
        elif self.appear(BEACON_LIST, offset=(20, 20), interval=2):
            if attack_mode == 'current_dossier_only':
                self.appear_then_click(ASH_QUIT, offset=(10, 10), interval=2)
                return True
            if self._check_beacon_point():
                self.device.click(META_BEGIN_ENTRANCE)
                logger.info('[META — бой] Запуск маяка')
            return True
        # Страница архива
        elif _server_support() \
                and self.appear(DOSSIER_LIST, offset=(20, 20), interval=2):
            if attack_mode != 'current' \
                    and self._check_dossier_point():
                if self.appear_then_click(META_BEGIN_ENTRANCE, offset=(20, 20), interval=2):
                    logger.info('[META — бой] Запуск архива')
                    return True
                else:
                    logger.info('[META — бой] Архив не выбран')
            self.appear_then_click(ASH_QUIT, offset=(10, 10), interval=2)
            return True
        # Неизвестная страница
        else:
            return True

    def _check_beacon_point(self) -> bool:
        """
        Проверить, набрано ли >= 100 очков маяка.

        Returns:
            bool: Достаточно ли очков для запуска боя.
        """
        if self.appear(META_BEACON_FLAG, offset=(180, 20)):
            META_BEACON_DATA.load_offset(META_BEACON_FLAG)
            return self.digit_ocr_point_and_check(META_BEACON_DATA.button, 100)
        return False

    def _check_dossier_point(self) -> bool:
        """
        Проверить, набрано ли >= 100 очков архива досье.

        Returns:
            bool: Достаточно ли очков для запуска боя.
        """
        if self.appear(META_DOSSIER_FLAG, offset=(180, 20)):
            META_DOSSIER_DATA.load_offset(META_DOSSIER_FLAG)
            return self.digit_ocr_point_and_check(META_DOSSIER_DATA.button, 100)
        return False

    def _get_state(self):
        """
        Определить текущее состояние экрана META.

        Returns:
            MetaState: Значение перечисления текущего состояния экрана.
        """
        # Неизвестная страница
        if not self._in_meta_page():
            return MetaState.UNDEFINED
        # Страница маяка или архива
        elif self.appear(BEACON_LIST, offset=(20, 20)) \
                or self.appear(DOSSIER_LIST, offset=(20, 20)):
            if self.appear(HELP_ENTER, offset=(30, 30)):
                return MetaState.ATTACKING
            elif self.appear(BEACON_REWARD, offset=(20, 20)):
                return MetaState.COMPLETE
            return MetaState.INIT
        elif self.appear(ASH_SHOWDOWN, offset=(30, 30)):
            return MetaState.INIT
        return MetaState.UNDEFINED

    def _in_meta_page(self):
        """Определить, открыт ли сейчас экран, связанный с META (главный, маяки или досье)."""
        return self.appear(ASH_SHOWDOWN, offset=(30, 30)) \
               or self.appear(BEACON_LIST, offset=(20, 20)) \
               or self.appear(DOSSIER_LIST, offset=(20, 20))

    def _ensure_meta_page(self, skip_first_screenshot=True):
        """
        Убедиться, что открыт экран META; если нет — перейти через клик по входу.

        Pages:
            in: page_reward
            out: in_meta
        """
        logger.info('[META — бой] Переход на экран атаки маяка')
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self._in_meta_page():
                logger.info('[META — бой] Экран META уже открыт')
                return True
            if self.handle_map_event():
                continue
            if self.appear_then_click(META_ENTRANCE, offset=(20, 300), interval=2):
                continue

    def ensure_dossier_page(self, skip_first_screenshot=True):
        """
        Убедиться, что открыт экран архива досье.

        Переходит в меню наград, открывает экран META и переключается на вкладку досье.

        Pages:
            in: page_reward
            out: in_meta, DOSSIER_LIST
        """
        self.ui_ensure(page_reward)
        self._ensure_meta_page()
        logger.info('[META — бой] Переход на экран архива META')
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear(DOSSIER_LIST, offset=(20, 20)):
                logger.info('[META — бой] Экран архива уже открыт')
                return True
            if self.handle_map_event():
                continue
            if self.appear(ASH_SHOWDOWN, offset=(30, 30)):
                self.device.click(META_MAIN_DOSSIER_ENTRANCE)
                continue

    def _begin_beacon(self):
        """Запустить процесс атаки маяка, перейдя на экран META."""
        logger.hr('Бой META')
        if not _server_support():
            logger.info("Текущий сервер пока не поддерживает архивные маяки и режим одного удара; обратитесь к разработчику")
        self._ensure_meta_page()
        self._attack_meta()

    def run(self):
        """Основной поток атаки маяка: переход на экран META, атака, получение наград, отсрочка до обновления сервера."""
        self.ui_ensure(page_reward)
        self._begin_beacon()
        self.ui_goto_main()

        with self.config.multi_set():
            for meta in self._meta_receive:
                MetaReward(self.config, self.device).run(category=meta)
            self._meta_receive = []
            self.config.task_delay(server_update=True)


class AshBeaconAssist(Meta):
    """Задача поддержки маяков Пепла: помощь по запросам других игроков."""

    def _attack_meta(self, skip_first_screenshot=True):
        """
        Провести бой поддержки маяка META.

        Ищет доступные маяки в списке, проверяет оставшиеся попытки и запускает атаку.

        Returns:
            bool: Найден ли доступный для атаки маяк.

        Pages:
            in: page_reward
            out: page_reward
        """
        timeout = Timer(3, count=9).start()
        appeared = False
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if not appeared and timeout.reached():
                logger.info('[META — поддержка] Не найден доступный для поддержки маяк META, задача отложена')
                break

            if self.handle_map_event():
                continue
            if self.appear(ASH_START, offset=(20, 20)):
                appeared = True
                remain_times = self.digit_ocr_point_and_check(BEACON_REMAIN, 1)
                if remain_times:
                    self._ensure_meta_level()
                    self._make_an_attack()
                else:
                    logger.info('[META — поддержка] Попытки поддержки исчерпаны, задача завершена')
                    break

        return appeared

    def _make_an_attack(self):
        """
        Провести один бой поддержки META.

        После боя подтверждает возврат на экран поддержки, обрабатывая случайные переходы на экран подготовки или главной страницы.

        Pages:
            in: in_meta_assist
            out: in_meta_assist
        """
        logger.hr('Бой поддержки META', level=2)

        def expected_end():
            # При случайном входе на экран подготовки к бою нажимаем «Назад»
            if self.appear(BATTLE_PREPARATION, offset=(30, 30), interval=2):
                logger.info('[META — поддержка] Случайно открыт экран подготовки к бою')
                self.device.click(BACK_ARROW)
                return False
            # После помощи могли перенаправить на собственный незавершённый маяк; возвращаемся к списку маяков
            if self.appear_then_click(BEACON_LIST, offset=(-20, -5, 300, 5), interval=2):
                return False
            # Вернулись на главную страницу META — нажимаем вход в маяк
            if self.appear(ASH_SHOWDOWN, offset=(30, 30), interval=2):
                logger.info('[META — поддержка] Бой завершён, выполнен возврат на экран противостояния META')
                self.device.click(META_MAIN_BEACON_ENTRANCE)
            # Уже вернулись на страницу поддержки
            if self._in_meta_assist_page():
                logger.info('[META — поддержка] Бой завершён, выполнен возврат на нужную страницу')
                return True

            return False

        # Выполняем бой
        combat = AshCombat(config=self.config, device=self.device)
        combat.combat(expected_end=expected_end, save_get_items=False, emotion_reduce=False)

    def _ensure_meta_level(self):
        """
        Выбрать маяк META, соответствующий требованиям уровня.

        Ожидает появления цифр уровня маяка, считывает уровень через OCR и при необходимости листает список (до 5 попыток).
        """
        # Ждём появления BEACON_TIER: при входе в список маяков уровень отображается не сразу
        tier = self.config.OpsiAshAssist_Tier
        logger.info(f'[META — поддержка] Поиск маяка META уровня {tier}.')
        for n in range(10):
            if self.image_color_count(BEACON_TIER, color=(0, 0, 0), threshold=221, count=50):
                break

            self.device.screenshot()
            if n >= 9:
                logger.warning('[META — поддержка] Истекло время ожидания отображения уровня маяка')
        # Выбираем маяк
        current = -1
        for _ in range(5):
            current = OCR_BEACON_TIER.ocr(self.device.image)
            if current >= tier:
                break
            else:
                self.device.click(BEACON_NEXT)
                self.device.sleep((0.3, 0.5))
                self.device.screenshot()
        if current < tier:
            logger.info(f'[META — поддержка] За 5 попыток маяк уровня {tier} не найден; используется текущий маяк')
        logger.info(f'[META — поддержка] Найден маяк уровня {current}.')

    def _in_meta_assist_page(self):
        """Определить, открыт ли сейчас экран поддержки маяка."""
        return self.appear(BEACON_MY, offset=(20, 20))

    def _ensure_meta_assist_page(self, skip_first_screenshot=True):
        """
        Убедиться, что открыт экран поддержки маяка.

        Переходит через вход META, обрабатывая различные промежуточные экраны.

        Pages:
            in: page_reward or in_meta
            out: in_meta_assist
        """
        logger.info('[META — поддержка] Переход на экран поддержки маяка')
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self._in_meta_assist_page():
                logger.info('[META — поддержка] Экран поддержки маяка уже открыт')
                return True
            if self.handle_map_event():
                continue
            if self.appear_then_click(META_ENTRANCE, offset=(20, 300), interval=2):
                continue
            if self.appear(ASH_SHOWDOWN, offset=(20, 20), interval=2):
                self.device.click(META_MAIN_BEACON_ENTRANCE)
                logger.info('[META — поддержка] Уже открыт главный экран META')
                continue
            if self.appear_then_click(BEACON_LIST, offset=(300, 20), interval=2):
                continue
            if self.appear_then_click(DOSSIER_LIST, offset=(20, 20), interval=2):
                logger.info('[META — поддержка] Уже открыт экран архива META')
                continue

    def _begin_meta_assist(self):
        """Запустить процесс поддержки маяка, перейдя на экран поддержки."""
        logger.hr('Поддержка META')
        self._ensure_meta_assist_page()
        return self._attack_meta(skip_first_screenshot=False)

    def run(self):
        """
        Основной поток задачи поддержки маяков META.

        При успешной помощи забирает награды и откладывает задачу до обновления сервера;
        если подходящих маяков не найдено, откладывает на 10-20 минут.
        """
        self.ui_ensure(page_reward)

        if self._begin_meta_assist():
            MetaReward(self.config, self.device).run()
            self.config.task_delay(server_update=True)
        else:
            self.config.task_delay(minute=(10, 20))
