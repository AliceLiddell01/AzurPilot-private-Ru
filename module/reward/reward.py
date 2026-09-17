"""Обработчик сбора наград, централизованно управляющий получением ресурсов и наград за задания.
Поддерживает получение нефти, монет, опыта и наград за выполненные задания.
"""

from module.base.button import ButtonGrid
from module.base.decorator import cached_property
from module.base.timer import Timer
from module.combat.assets import *
from module.logger import logger
from module.reward.assets import *
from module.ui.navbar import Navbar
from module.ui.page import page_main, page_mission, page_reward
from module.ui.ui import UI
from module.ui_white.assets import MISSION_NOTICE_WHITE


class Reward(UI):
    def reward_receive(self, oil, coin, exp):
        """
        Получение наград ресурсами (нефть, монеты, опыт).

        Args:
            oil (bool): Получать ли нефть.
            coin (bool): Получать ли монеты.
            exp (bool): Получать ли опыт.

        Returns:
            bool: Были ли получены награды.

        Pages:
            in: page_reward
            out: page_reward, при успешном сборе отображается info_bar
        """
        if not oil and not coin and not exp:
            return False

        logger.hr('Получение наград')
        logger.info(f'[Награды — получение] Нефть={oil}, монеты={coin}, опыт={exp}')
        confirm_timer = Timer(1, count=3).start()
        # Устанавливаем интервал кликов 0,3 с, потому что игра не успевает обрабатывать слишком быстрые нажатия.
        click_timer = Timer(0.3)
        for _ in self.loop():
            if oil and click_timer.reached() and self.appear_then_click(OIL, offset=(20, 50), interval=60):
                confirm_timer.reset()
                click_timer.reset()
                continue
            if coin and click_timer.reached() and self.appear_then_click(COIN, offset=(25, 50), interval=60):
                confirm_timer.reset()
                click_timer.reset()
                continue
            if exp and click_timer.reached() and self.appear_then_click(EXP, offset=(30, 50), interval=60):
                confirm_timer.reset()
                click_timer.reset()
                continue

            # End
            if confirm_timer.reached():
                break

        logger.info('[Награды — получение] Получение наград завершено')
        return True

    def _reward_get_state(self):
        if self.appear(MISSION_MULTI, offset=(20, 20)):
            return MISSION_MULTI
        if self.match_template_color(MISSION_SINGLE, offset=(50, 200)):
            return MISSION_SINGLE
        if self.appear(MISSION_EMPTY, offset=(20, 20)):
            return MISSION_EMPTY
        if self.appear(MISSION_UNFINISH, offset=(50, 200)):
            return MISSION_UNFINISH
        return None

    def _reward_mission_claim_click(self):
        """
        Нажатие для получения наград за задания.

        Returns:
            bool: Было ли нажато получение.

        Pages:
            in: page_mission, MISSION_MULTI или MISSION_SINGLE
            out: Неизвестное всплывающее окно
        """
        clicked = False
        click_interval = Timer(1, count=2)
        for _ in self.loop():
            if clicked and not self.ui_page_appear(page_mission):
                return clicked
            if click_interval.reached():
                if self.appear_then_click(MISSION_MULTI, offset=(20, 20)):
                    click_interval.reset()
                    clicked = True
                    continue
                if self.match_template_color(MISSION_SINGLE, offset=(50, 200)):
                    self.device.click(MISSION_SINGLE)
                    click_interval.reset()
                    clicked = True
                    continue
                if self.appear(MISSION_UNFINISH, offset=(50, 200)):
                    return clicked

    def _reward_mission_claim_receive(self):
        """
        Обработка всплывающих окон после нажатия получения наград за задания.

        Returns:
            Button | str: Объект Button или строка состояния.

        Pages:
            in: Неизвестное всплывающее окно
            out: page_mission
        """
        logger.info('[Награды — задания] Получение награды за задание')
        timeout = Timer(2, count=6).start()
        for _ in self.loop():
            if self.ui_page_appear(page_mission):
                state = self._reward_get_state()
                if state:
                    return state
                if timeout.reached():
                    logger.warning('[Награды — задания] Тайм-аут ожидания получения награды')
                    return 'timeout'
            else:
                timeout.reset()

            # click
            if self.appear_then_click(GET_ITEMS_1, offset=(30, 30), interval=1):
                continue
            if self.appear_then_click(GET_ITEMS_2, offset=(30, 30), interval=1):
                continue
            if self.appear_then_click(GET_SHIP, interval=1):
                continue
            if self.handle_mission_popup_ack():
                continue
            if self.handle_vote_popup():
                continue
            if self.handle_story_skip():
                continue
            if self.handle_popup_confirm('MISSION_REWARD'):
                continue

    def _reward_wait_mission_list(self):
        """
        Ожидание полной загрузки списка заданий.

        Pages:
            in: page_mission
            out: page_mission, любое состояние заданий или таймаут
        """
        timeout = Timer(1, count=2).start()
        for _ in self.loop():
            state = self._reward_get_state()
            if state:
                return state
            if timeout.reached():
                return 'timeout'

    def _reward_mission_collect(self):
        """
        Единая обработка сбора наград за задания на страницах «Все» и «Еженедельные».

        Returns:
            Button | str: Итоговое состояние, объект Button или строка состояния.
        """
        state = self._reward_wait_mission_list()
        while 1:
            logger.attr('Состояние задания', state)
            self.device.stuck_record_clear()
            self.device.click_record_clear()
            if state == 'timeout':
                logger.warning('[Награды — задания] Тайм-аут ожидания списка заданий')
                return state
            if state in [MISSION_EMPTY, MISSION_UNFINISH]:
                logger.info('[Награды — задания] Сбор наград за задания завершён')
                break
            elif state in [MISSION_MULTI, MISSION_SINGLE]:
                # Сбрасываем существующие таймеры интервалов для следующих ресурсов
                self.interval_clear([GET_ITEMS_1, GET_ITEMS_2, MISSION_MULTI, MISSION_SINGLE, GET_SHIP])
                self._reward_mission_claim_click()
                state = self._reward_mission_claim_receive()
                continue
            else:
                logger.warning('[Награды — задания] Пустое состояние задания; сбор завершён')

        return state

    def _reward_mission_all(self):
        """
        Получение наград за задания на странице «Все».

        Returns:
            bool: Было ли выполнено действие.
        """
        self.reward_side_navbar_ensure(upper=1)
        return self._reward_mission_collect()

    def _reward_mission_weekly(self):
        """
        Получение наград за задания на странице «Еженедельные».

        Returns:
            bool: Было ли выполнено действие.
        """
        if not self.image_color_count(MISSION_WEEKLY_RED_DOT, color=(206, 81, 66), threshold=221, count=20):
            logger.info('[Награды — задания] Красная точка еженедельных заданий отсутствует')
            return False

        self.reward_side_navbar_ensure(upper=5)
        return self._reward_mission_collect()

    def reward_mission_notice(self):
        """
        Проверка наличия индикатора выполненных заданий на главной странице.

        Returns:
            bool: Есть ли индикатор заданий.

        Pages:
            in: page_main
        """
        if self.appear(MISSION_NOTICE):
            logger.info('[Награды — задания] Обнаружено уведомление MISSION_NOTICE')
            return True
        if self.image_color_count(MISSION_NOTICE_WHITE, color=(214, 117, 99), threshold=221, count=20):
            logger.info('[Награды — задания] Обнаружено уведомление MISSION_NOTICE_WHITE')
            return True

        return False

    def reward_mission(self, daily=True, weekly=True):
        """
        Получение наград за задания.

        Args:
            daily (bool): Получать ли ежедневные награды.
            weekly (bool): Получать ли еженедельные награды.

        Returns:
            bool: Были ли получены награды.

        Pages:
            in: page_main
            out: page_mission
        """
        if not daily and not weekly:
            return False
        logger.hr('Награды за задания')
        if not self.reward_mission_notice():
            return False

        self.ui_goto(page_mission, skip_first_screenshot=True)

        if daily:
            self._reward_mission_all()
        if weekly:
            self._reward_mission_weekly()

    @cached_property
    def _reward_side_navbar(self):
        """
        Пункты боковой панели навигации:
           all.    (Все)
           main.   (Основные)
           side.   (Побочные)
           daily.  (Ежедневные)
           weekly. (Еженедельные)
           event.  (События)
        """
        reward_side_navbar = ButtonGrid(
            origin=(21, 118), delta=(0, 94.5),
            button_shape=(60, 75), grid_shape=(1, 6),
            name='REWARD_SIDE_NAVBAR')

        return Navbar(grids=reward_side_navbar,
                      active_color=(247, 255, 173),
                      inactive_color=(140, 162, 181))

    def reward_side_navbar_ensure(self, upper=None, bottom=None):
        """
        Обеспечение переключения боковой панели навигации на указанную страницу.
        Полная загрузка страницы обрабатывается вызывающей стороной отдельно.

        Args:
            upper (int):
                1  Все.
                2  Основные.
                3  Побочные.
                4  Ежедневные.
                5  Еженедельные.
                6  События.
            bottom (int):
                6  Все.
                5  Основные.
                4  Побочные.
                3  Ежедневные.
                2  Еженедельные.
                1  События.

        Returns:
            bool: Успешно ли переключена боковая панель навигации.
        """
        if self._reward_side_navbar.set(self, upper=upper, bottom=bottom):
            return True
        return False

    def run(self):
        """
        Pages:
            in: Любая страница
            out: page_main или page_mission, возможно с info_bar
        """
        self.ui_ensure(page_reward)
        self.reward_receive(
            oil=self.config.Reward_CollectOil,
            coin=self.config.Reward_CollectCoin,
            exp=self.config.Reward_CollectExp)
        self.ui_goto(page_main)
        self.reward_mission(daily=self.config.Reward_CollectMission,
                            weekly=self.config.Reward_CollectWeeklyMission)
        self.config.task_delay(server_update=True)
