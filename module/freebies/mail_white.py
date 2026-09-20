"""Модуль обработки почты в светлой теме UI (White Theme).

Обрабатывает полный цикл взаимодействия со страницей почты Azur Lane:
- Вход и выход со страницы почты
- Фильтрация по типу и пакетный сбор наград (заслуги, компенсации обслуживания, торговая лицензия)
- Пакетное удаление уже собранных писем
- Обработка всплывающих окон и диалогов подтверждения в светлой теме UI

Разработан для светлой темы UI и использует MailSelectSetting для управления
фильтрами содержимого писем (кубы, монеты, нефть, заслуги, алмазы и др.).
"""
from module.base.decorator import cached_property
from module.base.timer import Timer
from module.combat.assets import GET_ITEMS_1, GET_ITEMS_2
from module.freebies.assets import *
from module.logger import logger
from module.ui.page import GOTO_MAIN_WHITE, page_mail, page_main, page_main_white
from module.ui.setting import Setting
from module.ui.ui import UI


class MailSelectSetting(Setting):
    """Менеджер настроек фильтрации почты.

    Наследует Setting, управляет опциями фильтрации по типу содержимого писем.
    Определяет активность опции по темно-серому цвету кнопки (57, 56, 57).
    """

    def is_option_active(self, option: Button) -> bool:
        return self.main.image_color_count(option, color=(57, 56, 57), threshold=221, count=50)


class MailWhite(UI):
    """Обработчик почты для светлой темы UI.

    Отвечает за сбор и очистку почты в светлой теме интерфейса. Поддерживает:
    - Фильтрацию писем по типам содержимого (заслуги, компенсации, торговая лицензия).
    - Пакетный сбор наград из писем, подходящих под критерии.
    - Пакетное удаление уже прочитанных/собранных писем.

    Использует MailSelectSetting для управления условиями фильтрации:
    - mail_select_setting: фильтрация по типу (кубы, монеты, нефть, заслуги, алмазы).
    - mail_select_all_setting: режим «выбрать все» для пакетного удаления.

    Attributes:
        mail_select_setting: Экземпляр настроек фильтрации по типу содержимого (cached_property).
        mail_select_all_setting: Экземпляр настроек режима выбора всех писем (cached_property).
    """
    @cached_property
    def mail_select_setting(self):
        setting = MailSelectSetting('Mail', main=self)
        setting.reset_first = False
        setting.need_deselect = True
        setting.add_setting(
            setting='contains',
            option_buttons=[MAIL_SELECT_CUBE, MAIL_SELECT_COINS, MAIL_SELECT_OIL, MAIL_SELECT_MERIT, MAIL_SELECT_GEMS],
            option_names=['cube', 'coins', 'oil', 'merit', 'gems'],
            option_default='merit'
        )
        return setting

    @cached_property
    def mail_select_all_setting(self):
        setting = MailSelectSetting('MailAll', main=self)
        setting.reset_first = False
        setting.add_setting(
            setting='all',
            option_buttons=[MAIL_SELECT_ALL],
            option_names=['all'],
            option_default='all'
        )
        return setting

    def _mail_enter(self, skip_first_screenshot=True):
        """Войти на страницу почты.

        Returns:
            int: Есть ли письма в почте.

        Pages:
            in: page_main_white или MAIL_MANAGE
            out: MAIL_BATCH_CLAIM
        """
        logger.info('Вход в почту')
        self.interval_clear([
            MAIL_MANAGE
        ])
        timeout = Timer(0.6, count=1)
        has_mail = False
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            # End
            if self.appear(MAIL_BATCH_CLAIM, offset=(20, 20)):
                logger.info('Почта открыта')
                return True
            if self.appear(MAIL_WHITE_EMPTY, offset=(20, 20)):
                logger.info('Почта пуста')
                return False
            if not has_mail and self.appear(GOTO_MAIN_WHITE, offset=(20, 20)):
                timeout.start()
                if timeout.reached():
                    logger.info('Почта пуста: тайм-аут ожидания GOTO_MAIN_WHITE')
                    return False

            # Click
            if self.appear_then_click(MAIL_MANAGE, offset=(30, 30), interval=3):
                has_mail = True
                continue
            if self.ui_main_appear_then_click(page_mail, offset=(30, 30), interval=3):
                continue
            if self._handle_mail_reward():
                continue

    def _mail_quit(self, skip_first_screenshot=True):
        """Выйти со страницы почты.

        Pages:
            in: Любая страница в page_mail
            out: page_main_white
        """
        logger.info('Выход из почты')
        self.interval_clear([
            MAIL_BATCH_CLAIM,
            GOTO_MAIN_WHITE,
            GET_ITEMS_1,
            GET_ITEMS_2,
        ])
        self.popup_interval_clear()
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            # End
            if self.is_in_main():
                logger.info('Выход из почты на главную страницу')
                break

            # Click
            if self.handle_popup_confirm('MAIL_QUIT'):
                continue
            if self.appear(MAIL_BATCH_CLAIM, offset=(30, 30), interval=3):
                logger.info(f'{MAIL_BATCH_CLAIM} -> {MAIL_MANAGE}')
                self.device.click(MAIL_MANAGE)
                continue
            if self.appear_then_click(GOTO_MAIN_WHITE, offset=(30, 30), interval=3):
                continue
            if self._handle_mail_reward():
                continue

    def _handle_mail_reward(self):
        """Обработать диалог получения предметов после сбора наград почты.

        При появлении всплывающих окон GET_ITEMS_1 или GET_ITEMS_2 автоматически нажимает подтверждение
        для завершения процесса сбора наград.

        Returns:
            bool: Было ли обнаружено и обработано всплывающее окно предметов.
        """
        if self.appear(GET_ITEMS_1, offset=(30, 30), interval=3):
            logger.info(f'{GET_ITEMS_1} -> {MAIL_BATCH_CLAIM}')
            self.device.click(MAIL_BATCH_CLAIM)
            return True
        if self.appear(GET_ITEMS_2, offset=(30, 30), interval=3):
            logger.info(f'{GET_ITEMS_2} -> {MAIL_BATCH_CLAIM}')
            self.device.click(MAIL_BATCH_CLAIM)
            return True
        return False

    def _mail_claim_execute(self, skip_first_screenshot=True):
        """Выполнить пакетный сбор наград почты.

        Pages:
            in: MAIL_BATCH_CLAIM
            out: page_main_white, возможно с info_bar

        Returns:
            int: Успешно ли выполнен сбор.
        """
        self.handle_info_bar()
        self.interval_clear([
            MAIL_BATCH_CLAIM,
            GET_ITEMS_1,
            GET_ITEMS_2,
        ])
        self.popup_interval_clear()

        claimed = False
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            # End
            if claimed and self.appear(MAIL_BATCH_CLAIM, offset=(30, 30)):
                break
            # Click
            if not claimed and self.appear_then_click(MAIL_BATCH_CLAIM, offset=(30, 30), interval=3):
                continue
            if self.handle_popup_confirm('MAIL_CLAIM'):
                claimed = True
                continue
            if self._handle_mail_reward():
                claimed = True
                continue

        success = self.info_bar_count() > 0
        logger.info(f'Получение наград из почты успешно: {success}')
        return success

    def _mail_delete(self, skip_first_screenshot=True):
        """Пакетно удалить собранные письма.

        Pages:
            in: MAIL_BATCH_DELETE
            out: MAIL_BATCH_DELETE
        """
        self.handle_info_bar()
        self.interval_clear([
            MAIL_BATCH_DELETE
        ])
        self.popup_interval_clear()

        deleted = False
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            # End
            if deleted and self.appear(MAIL_BATCH_DELETE, offset=(30, 30)):
                break
            # Click
            if not deleted and self.appear_then_click(MAIL_BATCH_DELETE, offset=(30, 30), interval=3):
                continue
            if self.handle_popup_confirm('MAIL_CLAIM'):
                deleted = True
                continue
            if self._handle_mail_reward():
                continue

        # После успешного удаления писем или если удалять нечего, появляется info_bar
        return True

    def mail_claim(
            self,
            merit=True,
            maintenance=False,
            trade_license=False,
            delete=True,
    ):
        """Собрать награды из писем.

        Args:
            merit (bool): Собирать ли письма с заслугами.
            maintenance (bool): Собирать ли компенсации за технические работы.
            trade_license (bool): Собирать ли награды торговой лицензии.
            delete (bool): Удалять ли собранные письма.

        Pages:
            in: page_main_white или MAIL_MANAGE
            out: MAIL_BATCH_CLAIM
        """
        if not self._mail_enter():
            return

        if merit:
            logger.hr('Почта: заслуги', level=2)
            self._mail_enter()
            self.mail_select_setting.set(contains=['merit'])
            self._mail_claim_execute()
        if maintenance:
            logger.hr('Почта: компенсация за обслуживание', level=2)
            self._mail_enter()
            self.mail_select_setting.set(contains=['coins', 'oil'])
            self._mail_claim_execute()
            self._mail_enter()
            self.mail_select_setting.set(contains=['coins', 'oil', 'gems'])
            self._mail_claim_execute()
        if trade_license:
            logger.hr('Почта: торговая лицензия', level=2)
            self._mail_enter()
            self.mail_select_setting.set(contains=['coins', 'oil', 'cube'])
            self._mail_claim_execute()
        if delete:
            logger.hr('Удаление почты', level=2)
            self._mail_enter()
            self.mail_select_all_setting.set(contains=['all'])
            self._mail_delete()

        self._mail_quit()

    def run(self):
        merit = self.config.Mail_ClaimMerit
        maintenance = self.config.Mail_ClaimMaintenance
        trade_license = self.config.Mail_ClaimTradeLicense
        delete = self.config.Mail_DeleteCollected
        logger.info(f'[Бонусы — почта] Награды: заслуги={merit}, компенсация за обслуживание={maintenance}, '
                    f'торговая лицензия={trade_license}, удаление={delete}')
        if not merit and not maintenance and not trade_license:
            logger.warning('Нечего получать')
            return False

        # Необходимо использовать светлую тему UI
        self.ui_ensure(page_main)
        if self.appear(page_main_white.check_button, offset=(30, 30)):
            logger.info('Открыта светлая главная страница')
            pass
        elif self.appear(page_main.check_button, offset=(5, 5)):
            logger.info('Открыта главная страница')
            pass
        else:
            logger.warning('[Бонусы — почта] Неизвестная главная страница; невозможно открыть почту')
            return False

        # Получение наград
        self.mail_claim(
            merit=merit,
            maintenance=maintenance,
            trade_license=trade_license,
            delete=delete,
        )
        return True
