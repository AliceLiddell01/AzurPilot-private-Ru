"""Модуль задач личных покоев (Private Quarters).

Автоматизирует ежедневные задачи в личных покоях, включая покупки в магазине и взаимодействие с кораблями.

Основные возможности:
    - Покупка еженедельных товаров в магазине (розы за монеты, торт за алмазы).
    - Ежедневное интимное взаимодействие с назначенным кораблем.
    - Распознавание OCR оставшихся ежедневных взаимодействий.
    - Переход в комнату корабля и выполнение серии взаимодействий.

Механика взаимодействия с кораблями:
    - Фиксированный ежедневный лимит взаимодействий близости.
    - Для взаимодействия необходимо войти в комнату соответствующего корабля.
    - Взаимодействие включает варианты диалогов и тактильное взаимодействие.
    - Доступные корабли: Anchorage, Noshiro, Sirius, New Jersey, Taihou, Ägir, Admiral Nakhimov.

Механика магазина:
    - Розы (Roses): еженедельный лимитированный товар за монеты (~24 000+).
    - Торт (Cake): еженедельный лимитированный товар за алмазы (~210+).
    - TW-сервер временно не поддерживает функционал магазина.

Иерархия наследования:
    - PQInteract: логика взаимодействия с кораблями (навигация по комнатам, диалоги, касания).
    - PQShop: логика покупок в магазине (фильтрация товаров, подтверждение покупок).

Ограничения серверов:
    - Некоторые корабли недоступны на определенных серверах (настраивается в not_supported_filter).
    - TW-сервер не поддерживает магазин.

Pages:
    Страница личных покоев: page_private_quarters
    Страница меню общежития: page_dormmenu
"""

import module.config.server as server
from module.base.timer import Timer
from module.logger import logger
from module.private_quarters.assets import *
from module.private_quarters.interact import PQInteract
from module.private_quarters.shop import PQShop
from module.ui.page import page_private_quarters, page_dormmenu


class PrivateQuarters(PQInteract, PQShop):
    """Обработчик задач личных покоев.

    Управляет выполнением ежедневных сценариев личных покоев, объединяя
    взаимодействие с кораблями (PQInteract) и покупки в магазине (PQShop).

    Основной поток:
        1. Из любой страницы перейти в меню общежития, затем войти в личные покои.
        2. Если включена покупка еженедельных товаров — зайти в магазин и купить розы/торт.
        3. Если включено взаимодействие — проверить остаток попыток и зайти в комнату цели.

    Attributes:
        not_supported_filter (dict): Список неподдерживаемых кораблей по серверам.

    Параметры конфигурации:
        PrivateQuarters_BuyRoses: Покупать ли еженедельные розы.
        PrivateQuarters_BuyCake: Покупать ли еженедельный торт.
        PrivateQuarters_TargetInteract: Выполнять ли взаимодействие с кораблем.
        PrivateQuarters_TargetShip: Имя целевого корабля (в нижнем регистре, например 'sirius').
    """
    # Key: str, server name
    # Value: list[str]
    not_supported_filter = {
        'cn': ('nakhimov'),
        'en': (),
        'jp': ('nakhimov'),
        'tw': ('taihou', 'nakhimov'),
    }

    def _pq_get_daily_count(self, retry=3):
        """Получить оставшееся число ежедневных взаимодействий с повторными попытками.

        На мощных ПК начальный снимок может быть размыт или задержан,
        поэтому выполняется несколько повторных считываний для точного результата.

        Args:
            retry (int): Максимальное количество попыток.

        Returns:
            int: Оставшееся число взаимодействий (0 — попытки исчерпаны).

        Pages:
            in: Главная страница личных покоев
        """
        count = self.status_get_daily_count()
        get_timer = Timer(1.5, count=3).start()
        skip_first_screenshot = True
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            # Условие завершения: получено ненулевое число попыток либо исчерпаны повторы и подтверждён ноль
            if count != 0 or retry == 0:
                return count

            # По истечении таймера повторно считываем число ежедневных попыток
            if get_timer.reached():
                count = self.status_get_daily_count()
                get_timer.reset()
                retry -= 1

    def _pq_shop_enter(self):
        """Войти в магазин личных покоев и перейти на вкладку подарков Сириус.

        Pages:
            in: Главная страница личных покоев
            out: Магазин личных покоев — Сириус — Подарки
        """
        # Входим в магазин
        self.ui_click(
            click_button=PRIVATE_QUARTERS_SHOP_ENTER,
            check_button=PRIVATE_QUARTERS_SHOP_CHECK,
            appear_button=page_private_quarters.check_button,
            offset=(20, 20),
            skip_first_screenshot=True
        )

        # Переключаемся на раздел Сириус
        self.shop_left_navbar_ensure(2)

        # Переключаемся на вкладку подарков
        self.shop_bottom_navbar_ensure(2)

    def _pq_shop_exit(self):
        """Выйти из магазина личных покоев и вернуться на главную страницу покоев.

        Pages:
            in: Магазин личных покоев
            out: Главная страница личных покоев
        """
        self.ui_click(
            click_button=PRIVATE_QUARTERS_SHOP_BACK,
            check_button=page_private_quarters.check_button,
            appear_button=PRIVATE_QUARTERS_SHOP_CHECK,
            offset=(20, 20),
            skip_first_screenshot=True
        )

    def pq_shop_weekly_items(self):
        """Купить еженедельные товары в магазине.

        Розы требуют 24 000+ монет, торт требует 210+ алмазов.
        При нехватке средств покупка пропускается до следующего дня.

        Pages:
            in: Главная страница личных покоев
            out: Главная страница личных покоев
        """
        logger.hr(f'[Личные покои] Получение еженедельных предметов', level=2)

        # Входим в магазин
        self._pq_shop_enter()

        # Выполняем покупку
        self.shop_buy()

        # Выходим из магазина
        self._pq_shop_exit()

    def pq_execute_interact(self, target_ship):
        """Выполнить процесс взаимодействия с целевым кораблем.

        После проверки корректности цели входит в комнату и запускает цепочку взаимодействий.

        Args:
            target_ship (str): Имя целевого корабля (в нижнем регистре, например 'sirius').

        Pages:
            in: Главная страница личных покоев
            out: Главная страница личных покоев
        """
        # Проверяем, доступна ли выбранная цель
        target_title = target_ship.title().replace('_', ' ')
        if target_ship not in self.available_targets:
            logger.error(f'Неподдерживаемый целевой корабль: {target_title}; подзадачу продолжить невозможно')
            return

        # Входим в комнату цели, максимум 3 попытки
        if not self.pq_goto_room(target_ship, retry=3):
            return

        # Выполняем сценарий взаимодействия
        self.pq_interact()

    def pq_run(self, buy_roses, buy_cake, target_interact, target_ship):
        """Выполнить ежедневный цикл личных покоев.

        Включает покупку еженедельных товаров (розы/торт) и взаимодействие с кораблем.

        Args:
            buy_roses (bool): Покупать ли еженедельные розы.
            buy_cake (bool): Покупать ли еженедельный торт.
            target_interact (bool): Взаимодействовать ли с кораблем.
            target_ship (str): Имя целевого корабля.

        Pages:
            in: Главная страница личных покоев
            out: Главная страница личных покоев
        """
        logger.hr('Запуск личных покоев', level=1)
        target_title = target_ship.title().replace('_', ' ')
        logger.info(f'[Личные покои] Конфигурация задачи: покупать розы={buy_roses}, '
                    f'покупать торт={buy_cake}, '
                    f'взаимодействовать с кораблём={target_interact}, '
                    f'целевой корабль={target_title}')

        # Заходим в магазин за еженедельными предметами
        if self.shop_filter:
            if server.server not in ['tw']:
                self.pq_shop_weekly_items()
            else:
                logger.info(f'[Личные покои] Сервер {server.server} не поддерживает функцию магазина')

        # Выполняем взаимодействие с кораблём
        if target_interact:
            # Ensure target is supported for server
            # Update `not_supported_filter` to enable a target
            if target_ship in self.not_supported_filter[server.server]:
                logger.info(f'[Личные покои] Целевой корабль {target_ship} недоступен на сервере {server.server}')
                return

            # Получаем оставшееся число ежедневных попыток; при 0 выходим
            count = self._pq_get_daily_count(retry=3)
            if count == 0:
                logger.info('Ежедневные попытки близости исчерпаны; выход из подзадачи')
                return

            # Выполняем взаимодействие
            self.pq_execute_interact(target_ship)

    def run(self):
        """Точка входа задачи личных покоев.

        Из любой страницы переходит в меню общежития, заходит в личные покои и выполняет цикл.

        Pages:
            in: Любая страница
            out: page_main, возможно с info_bar
        """
        self.ui_ensure(page_dormmenu)
        self.ui_goto(page_private_quarters, get_ship=False)
        self.handle_info_bar()
        self.pq_run(
            buy_roses=self.config.PrivateQuarters_BuyRoses,
            buy_cake=self.config.PrivateQuarters_BuyCake,
            target_interact=self.config.PrivateQuarters_TargetInteract,
            target_ship=self.config.PrivateQuarters_TargetShip
        )

        self.config.task_delay(server_update=True)
