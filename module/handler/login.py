"""Обработчик входа в игру.

Управляет входом в Azur Lane и перезапуском игры, включая:
- запуск приложения и обнаружение экрана входа;
- обработку различных окон входа (объявления, события, награды за вход);
- восстановление после сбоя или зависания игры;
- обработку ошибок подключения к серверу.

Поток входа покрывает состояния UI после запуска игры, обнаруживает и
обрабатывает возможные окна и диалоги через цикл свежих screenshots, а затем
подтверждает возврат игры на главный экран.

Класс наследуется от UI и использует его навигацию для переходов между
страницами и обработки окон.
"""

# Логика smart restart добавлена поверх оригинального login.py.
# Она обрабатывает окна и объявления входа, а также восстанавливает приложение
# после сбоя.
# Последнее обновление: 2025-08-25 20:41
from math import isfinite
from time import monotonic

import numpy as np
from scipy.signal import find_peaks

# Эта последовательность импорта нужна для совместимости legacy dependencies.
# isort: off
# Исправить pkg_resources перед импортом adbutils и uiautomator2.
from module.device.pkg_resources import get_distribution
from uiautomator2 import UiObject
from uiautomator2.exceptions import XPathElementNotFoundError
from uiautomator2.xpath import XPath, XPathSelector
# isort: on

_ = get_distribution

from module.base.button import Button
from module.base.timer import Timer
from module.base.utils import color_similarity_2d, crop
from module.config import server
from module.handler.assets import *
from module.logger import logger
from module.map.assets import *
from module.ui.assets import *
from module.ui.page import page_campaign_menu
from module.ui.ui import UI


class LoginHandlerTimeoutError(TimeoutError):
    """Ожидаемое завершение bounded login flow по тайм-ауту."""


class LoginHandler(UI):
    """Обработчик входа и перезапуска игры.

    Обрабатывает вход после запуска игры, автоматически закрывает окна,
    объявления и награды за вход, а также восстанавливает игру после сбоя.

    Основные методы:
    - _handle_app_login(): полный flow от любой страницы до главного экрана;
    - app_restart(): перезапуск приложения;
    - handle_app_login(): вход с восстановлением screenshot interval.
    """

    def _handle_app_login(self, timeout_seconds: float | None = None):
        """
        Pages:
            in: любая страница
            out: page_main

        Raises:
            ValueError: timeout_seconds имеет неподдерживаемое значение.
            GameStuckError: игра зависла.
            GameTooManyClickError: превышено число нажатий.
            GameNotRunningError: игра не запущена.
            LoginHandlerTimeoutError: истёк bounded тайм-аут входа.
        """
        if timeout_seconds is not None and (
            type(timeout_seconds) is not float
            or not isfinite(timeout_seconds)
            or timeout_seconds < 0
        ):
            raise ValueError(
                'timeout_seconds должен быть неотрицательным конечным float или None'
            )
        deadline = (
            None
            if timeout_seconds is None
            else monotonic() + timeout_seconds
        )

        logger.hr('Вход в приложение')

        confirm_timer = Timer(1.5, count=4).start()
        orientation_timer = Timer(5)
        login_success = False
        self.device.stuck_record_clear()
        self.device.click_record_clear()

        while 1:
            if deadline is not None and monotonic() >= deadline:
                raise LoginHandlerTimeoutError(
                    'Истёк bounded тайм-аут входа в приложение.'
                )
            # Проверить поворот экрана устройства.
            if not login_success and orientation_timer.reached():
                # После запуска приложения ориентация экрана может измениться.
                self.device.get_orientation()
                orientation_timer.reset()

            self.device.screenshot()
            if deadline is not None and monotonic() >= deadline:
                raise LoginHandlerTimeoutError(
                    'Истёк bounded тайм-аут входа в приложение.'
                )

            # Условие завершения.
            if self.is_in_main():
                if confirm_timer.reached():
                    logger.info('[Вход] Подтверждение перехода на главный экран')
                    break
            else:
                confirm_timer.reset()

            # Обработка входа.
            if self.match_template_color(LOGIN_CHECK, offset=(30, 30), interval=5):
                self.device.click(LOGIN_CHECK)
                if not login_success:
                    logger.info('[Вход] Вход выполнен успешно')
                    login_success = True
            if self.appear(ANDROID_NO_RESPOND, offset=(30, 30), interval=5):
                logger.warning('[Вход] Эмулятор не отвечает')
                self.device.click_record_add(ANDROID_NO_RESPOND)
                self.device.click_record_check()
                self.device.click(ANDROID_NO_RESPOND, control_check=False)
                continue
            if self.appear_then_click(LOGIN_ANNOUNCE, offset=(30, 30), interval=5):
                continue
            if self.appear_then_click(LOGIN_ANNOUNCE_2, offset=(30, 30), interval=5):
                continue
            if self.appear(EVENT_LIST_CHECK, offset=(30, 30), interval=5):
                self.device.click(BACK_ARROW)
                continue
            # Обновление и обслуживание.
            if self.appear_then_click(MAINTENANCE_ANNOUNCE, offset=(30, 30), interval=5):
                continue
            if self.appear_then_click(LOGIN_GAME_UPDATE, offset=(30, 30), interval=5):
                continue
            if server.server == 'cn' and not login_success and self.handle_cn_user_agreement():
                continue
            # Возвращение пользователя.
            if self.appear_then_click(LOGIN_RETURN_SIGN, offset=(30, 30), interval=5):
                continue
            if self.appear_then_click(LOGIN_RETURN_INFO, offset=(30, 30), interval=5):
                continue
            if self.appear_then_click(AVATAR_EXPIRED, offset=(30, 30), interval=5):
                continue
            # Обработка окон.
            if self.handle_popup_confirm('LOGIN'):
                continue
            if self.handle_urgent_commission():
                continue
            # Окна главного экрана.
            if self.ui_page_main_popups(get_ship=login_success):
                continue
            # Всегда пытаться вернуться на главный экран.
            if self.appear_then_click(GOTO_MAIN, offset=(30, 30), interval=5):
                continue

        return True

    _user_agreement_timer = Timer(1, count=2)

    def handle_cn_user_agreement(self):
        if not self._user_agreement_timer.reached():
            return False

        right = self.image_color_button(
            area=(640, 360, 1280, 720), color=(78, 189, 234),
            color_threshold=245, encourage=25, name='AGREEMENT_CONFIRM')
        if right is None:
            return False
        # С 2026-04-17 прокрутка не требуется: перед подтверждением достаточно
        # выполнить простой swipe.
        # Синяя кнопка только справа означает кнопку подтверждения.
        # Синие кнопки с обеих сторон означают центральное подтверждение входа.
        left = self.image_color_button(
            area=(0, 360, 640, 720), color=(78, 189, 234),
            color_threshold=245, encourage=25, name='AGREEMENT_CONFIRM')
        if left is None:
            # Пользовательское соглашение.
            # Выполнить swipe в центральной области экрана.
            box = (350, 230, 920, 430)
            self.device.swipe_vector((0, -150), box, name='AGREEMENT_SCROLL')
            self.device.swipe_vector((0, -150), box, name='AGREEMENT_SCROLL')
            self.device.click(right)
            self._user_agreement_timer.reset()
            return True
        else:
            # Подтверждение входа пользователя.
            self.device.click(right)
            self._user_agreement_timer.reset()
            return True

    def handle_app_login(self, timeout_seconds: float | None = None):
        """
        Обработать поток входа в приложение.

        Returns:
            Ничего: успех подтверждается состоянием UI вызывающим кодом.

        Raises:
            GameStuckError: игра зависла.
            GameTooManyClickError: превышено число нажатий.
            GameNotRunningError: игра не запущена.
        """
        logger.info('[Вход] Обработка входа в приложение')
        self.device.screenshot_interval_set(1.0)
        try:
            self._handle_app_login(timeout_seconds=timeout_seconds)
        finally:
            self.device.screenshot_interval_set()

    def app_stop(self):
        logger.hr('Остановка приложения')
        self.device.app_stop()

    def app_start(self, timeout_seconds: float | None = None):
        logger.hr('Запуск приложения')
        self.device.app_start()
        self.handle_app_login(timeout_seconds=timeout_seconds)
        # self.ensure_no_unfinished_campaign()

    # def app_restart(self):
    #     logger.hr('App restart')
    #     self.device.app_stop()
    #     self.device.app_start()
    #     self.handle_app_login()
    #     # self.ensure_no_unfinished_campaign()
    #     self.config.task_delay(server_update=True)

    def app_restart(self):
        logger.hr('Перезапуск приложения')
        # Ограниченный цикл повторных попыток перезапуска.
        RESTART_TRIES = 4
        FIRST_TRY_WAIT_SECONDS = 30
        SUBSEQUENT_TRY_WAIT_SECONDS = 20

        is_restart_success = False

        clear_cache = getattr(self.config, 'Restart_ClearCache', False)
        for i in range(RESTART_TRIES):
            logger.info(f"[Перезапуск] Попытка перезапуска приложения {i + 1}/{RESTART_TRIES}...")
            self.device.app_stop()
            if clear_cache:
                self.device.app_clear()
            self.device.sleep(3)
            self.device.app_start()
            wait_seconds = FIRST_TRY_WAIT_SECONDS if i == 0 else SUBSEQUENT_TRY_WAIT_SECONDS
            logger.info(f"[Перезапуск] Ожидание {wait_seconds} с для запуска и стабилизации приложения...")
            self.device.sleep(wait_seconds)

            # Проверить, запущено ли приложение.
            if self.device.app_is_running():
                logger.info("[Перезапуск] Приложение успешно запущено и работает")
                is_restart_success = True
                break  # Успешный запуск: выйти из цикла.
            else:
                logger.warning(f"[Перезапуск] Попытка {i + 1} не удалась: после запуска приложение не работает (возможно, произошёл сбой)")
                if i < RESTART_TRIES - 1:
                    logger.info("[Перезапуск] Повторная попытка...")

        # Если все попытки завершились неудачей, передать управление пользователю.
        if not is_restart_success:
            logger.critical(f"[Перезапуск] Выполнено {RESTART_TRIES} повторных попыток, но приложение всё ещё не запускается")
            from module.exception import RequestHumanTakeover
            raise RequestHumanTakeover("[Перезапуск] Не удалось перезапустить приложение после нескольких попыток")
        self.handle_app_login()
        # self.ensure_no_unfinished_campaign()

    def ensure_no_unfinished_campaign(self, confirm_wait=3):
        """
        Pages:
            in: page_main
            out: page_main

        Убедиться, что нет незавершённого боя; при наличии выйти из него.
        """

        def ensure_campaign_retreat():
            if self.appear_then_click(WITHDRAW, offset=(30, 30), interval=5):
                return True
            if self.handle_popup_confirm('WITHDRAW'):
                return True

        def in_campaign():
            return self.appear(CAMPAIGN_CHECK, offset=(30, 30)) \
                   or self.appear(CAMPAIGN_MENU_CHECK, offset=(30, 30)) \
                   or self.appear(EVENT_CHECK, offset=(30, 30)) \
                   or self.appear(SP_CHECK, offset=(30, 30))

        skip_first_screenshot = True
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            # Условие завершения.
            if in_campaign():
                break

            # Допустимые нажатия.
            if self.ui_main_appear_then_click(page_campaign_menu, interval=3):
                continue
            if ensure_campaign_retreat():
                continue

        self.ui_goto_main()

    def handle_user_agreement(self, xp, hierarchy):
        """
        Обработать окно пользовательского соглашения (только CN server).

        В CN client из-за ошибки соглашение и политика конфиденциальности могут
        появляться повторно после принятия. Метод пролистывает текст вниз и
        нажимает кнопку согласия.

        Returns:
            Было ли обработано окно соглашения.
        """

        if server.server == 'cn':
            area_wait_results = self.get_for_any_ele([
                XPS('//*[@text="sdk协议"]', xp, hierarchy),
                XPS('//*[@content-desc="sdk协议"]', xp, hierarchy)])
            if area_wait_results is False:
                return False
            agree_wait_results = self.get_for_any_ele([
                XPS('//*[@text="同意"]', xp, hierarchy),
                XPS('//*[@content-desc="同意"]', xp, hierarchy)])
            start_padding_results = self.get_for_any_ele([
                XPS('//*[@text="隐私政策"]', xp, hierarchy), XPS('//*[@content-desc="隐私政策"]', xp, hierarchy),
                XPS('//*[@text="用户协议"]', xp, hierarchy), XPS('//*[@content-desc="用户协议"]', xp, hierarchy)])
            start_margin_results = self.get_for_any_ele([
                XPS('//*[@text="请滑动阅读协议内容"]', xp, hierarchy),
                XPS('//*[@content-desc="请滑动阅读协议内容"]', xp, hierarchy)])

            test_image_original = self.device.image
            image_handle_crop = crop(
                test_image_original, (start_padding_results[2], 0, start_margin_results[2], 720), copy=False)
            # Image.fromarray(image_handle_crop).show()
            sims = color_similarity_2d(image_handle_crop, color=(182, 189, 202))
            points = np.sum(sims >= 255)
            if points == 0:
                return False
            sims_height = np.mean(sims, axis=1)
            # pyplot.plot(sims_height, color='r')
            # pyplot.show()
            peaks, __ = find_peaks(sims_height, height=225)
            if len(peaks) == 2:
                peaks = (peaks[0] + peaks[1]) / 2
            start_pos = [(start_padding_results[2] + start_margin_results[2]) / 2, float(peaks)]
            end_pos = [(start_padding_results[2] + start_margin_results[2]) / 2, area_wait_results[3]]
            logger.info("[Вход — соглашение] Результат поиска расположения пользовательского соглашения: " + ', '.join(f'{pos:.2f}' for pos in start_pos))
            logger.info("[Вход — соглашение] Ожидаемая область пользовательского соглашения: " + 'x:963-973, y:259-279')

            self.device.drag(start_pos, end_pos, segments=2, shake=(0, 25), point_random=(0, 0, 0, 0),
                             shake_random=(0, -5, 0, 5))
            AGREE = Button(area=agree_wait_results, color=(), button=agree_wait_results, name='AGREE')
            self.device.click(AGREE)
            return True

    def handle_user_login(self, xp, hierarchy) -> bool:
        """Обработать нажатие кнопки входа пользователя."""
        login_wait_results = self.get_for_any_ele([
            XPS('//*[@text="登录"]', xp, hierarchy),
            XPS('//*[@content-desc="登录"]', xp, hierarchy)])
        if login_wait_results is False:
            return False
        else:
            USER_LOGIN_BTN = Button(area=login_wait_results, color=(), button=login_wait_results, name='USER_LOGIN_BTN')
            self.device.click(USER_LOGIN_BTN)
            return True

    @staticmethod
    def get_for_any_ele(list_u2_path: list) -> bool | tuple:
        """
        Найти первый существующий элемент среди XPath и UiObject.

        Args:
            list_u2_path: список UiObject или XPathSelector длиной не менее 1.

        Returns:
            False, если элемент не найден; tuple с границами элемента — иначе.
        """
        for path in list_u2_path:
            try:
                if isinstance(path, UiObject):
                    if path.exists():
                        return path.bounds()
                    elif not path.exists():
                        continue
                elif isinstance(path, XPathSelector):
                    if path.exists:
                        return path.bounds
                    elif not path.exists:
                        continue
            except XPathElementNotFoundError:
                continue
        return False

    def get_cn_xp_hierarchy(self) -> tuple:
        d = self.device.u2
        xp = XPath(d)
        hierarchy = d.dump_hierarchy()
        return xp, hierarchy


class XPS(XPathSelector):
    def __init__(self, xpath, parent, source):
        super().__init__(parent, xpath, source)
