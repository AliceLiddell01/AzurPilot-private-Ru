"""Модуль события больницы.

Обеспечивает автоматизацию события больницы в Azur Lane, включая:
- Обнаружение красной точки и автоматическое получение ежедневных наград
- Переключение вкладок системы улик (Локации / Персонажи)
- Обход и выбор элементов списка реплик (aside)
- Вход в расследования (invest) и проведение боев
- Автоматический сбор наград за расследования
- Прокрутку списка реплик
- Корректный выход и отложенный перезапуск при нехватке топлива

Событие больницы — исследовательское событие, где игрок проводит расследования,
выбирая реплики различных локаций и персонажей, каждое из которых включает сбор улик и бои.
"""
from module.base.timer import Timer
from module.base.utils import random_rectangle_vector
from module.config.config import TaskEnd
from module.event_hospital.assets import *
from module.event_hospital.clue import HospitalClue
from module.event_hospital.combat import HospitalCombat
from module.exception import OilExhausted, ScriptEnd
from module.logger import logger
from module.ui.page import page_hospital, page_campaign_menu
from module.ui.switch import Switch


class HospitalSwitch(Switch):
    """Переключатель вкладок события больницы."""

    def get(self, main):
        """Получает текущее состояние вкладки.

        Определяет активную вкладку по цвету подсветки кнопок вкладок.

        Args:
            main: Экземпляр модуля для анализа цвета изображения.

        Returns:
            str: Название состояния; при отсутствии совпадений возвращает 'unknown'.
        """
        for data in self.state_list:
            if main.image_color_count(data['check_button'], color=(33, 77, 189), threshold=221, count=100):
                return data['state']

        return 'unknown'


HOSPITAL_TAB = HospitalSwitch('HOSPITAL_ASIDE', is_selector=True)
HOSPITAL_TAB.add_state('LOCATION', check_button=TAB_LOCATION)
HOSPITAL_TAB.add_state('CHARACTER', check_button=TAB_CHARACTER)


class Hospital(HospitalClue, HospitalCombat):
    """Главный контроллер события больницы.

    Объединяет возможности обработки улик (HospitalClue) и боев (HospitalCombat),
    реализуя полный цикл автоматизации события больницы.

    Порядок работы:
    1. Проверка доступности события, переход на страницу события
    2. Получение ежедневной награды (красная точка -> экран наград -> сбор -> выход)
    3. Вход в систему улик, обход всех реплик во вкладках локаций и персонажей
    4. Выполнение расследований для каждой реплики (вход -> бой -> сбор наград)
    5. Прокрутка списка реплик во вкладке персонажей для доступа к дополнительным репликам

    Attributes:
        HOSPITAL_TAB (HospitalSwitch): Переключатель вкладок с поддержкой состояний LOCATION и CHARACTER.
    """

    def daily_red_dot_appear(self):
        """Проверяет наличие красной точки ежедневной награды."""
        return self.image_color_count(DAILY_RED_DOT, color=(189, 69, 66), threshold=221, count=35)

    def daily_reward_receive_appear(self):
        """Проверяет доступность кнопки сбора ежедневной награды для нажатия."""
        return self.image_color_count(DAILY_REWARD_RECEIVE, color=(41, 73, 198), threshold=221, count=200)

    def is_in_daily_reward(self, interval=0):
        """Проверяет, находится ли экран в интерфейсе ежедневных наград."""
        return self.match_template_color(HOSIPITAL_CLUE_CHECK, offset=(30, 30), interval=interval)

    def daily_reward_receive(self):
        """Получает ежедневную награду.

        При обнаружении красной точки входит на экран наград, забирает их и выходит.

        Returns:
            bool: Удалось ли получить награду.

        Pages:
            in: page_hospital
        """
        if self.daily_red_dot_appear():
            logger.info('Красная точка ежедневной награды появилась')
        else:
            logger.info('Красной точки ежедневной награды нет')
            return False

        logger.hr('Получение ежедневной награды', level=2)
        # Входим на экран наград
        logger.info('Вход в ежедневные награды')
        skip_first_screenshot = True
        self.interval_clear(page_hospital.check_button)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()
            if self.is_in_daily_reward():
                break
            if self.ui_page_appear(page_hospital, interval=2):
                logger.info(f'{page_hospital} -> {HOSPITAL_GOTO_DAILY}')
                self.device.click(HOSPITAL_GOTO_DAILY)
                continue

        # Получаем награду
        logger.info('Получение ежедневной награды')
        skip_first_screenshot = True
        self.interval_clear(HOSIPITAL_CLUE_CHECK)
        timeout = Timer(1.5, count=6).start()
        clicked = False
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()
            if timeout.reached():
                logger.warning('Тайм-аут получения ежедневной награды')
                break
            if clicked and self.is_in_daily_reward():
                if not self.daily_reward_receive_appear():
                    break
            if self.is_in_daily_reward(interval=2):
                if self.daily_reward_receive_appear():
                    self.device.click(DAILY_REWARD_RECEIVE)
                    continue
            if self.handle_get_items():
                timeout.reset()
                clicked = True
                continue

        # Выходим с экрана наград
        logger.info('Выход из ежедневных наград')
        skip_first_screenshot = True
        self.interval_clear(HOSIPITAL_CLUE_CHECK)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.ui_page_appear(page_hospital):
                break
            if self.is_in_daily_reward(interval=2):
                self.device.click(HOSIPITAL_CLUE_CHECK)
                logger.info(f'is_in_daily_reward -> {HOSIPITAL_CLUE_CHECK}')
                continue

        return True

    def loop_invest(self):
        """Обходит все расследования на текущей странице и выполняет бои.

        После боя выбор реплики сбрасывается, требуется выбрать её заново.
        """
        self.config.override(Fleet_FleetOrder='fleet1_all_fleet2_standby')
        while 1:
            logger.hr('Цикл исследований госпиталя', level=2)
            # Проверка планировщика; может выбросить ScriptEnd
            self.emotion.check_reduce(battle=1)

            entered = self.invest_enter()
            if not entered:
                break
            self.hospital_combat()

            # Проверка планировщика; может выбросить TaskEnd
            if self.config.task_switched():
                self.config.task_stop()

            # После боя реплика сбрасывается; выходим, чтобы выбрать её заново
            break

        self.claim_invest_reward()
        logger.info('Цикл исследований госпиталя завершён')

    def invest_reward_appear(self) -> bool:
        """Проверяет появление кнопки сбора награды за исследование."""
        return self.image_color_count(INVEST_REWARD_RECEIVE, color=(33, 77, 189), threshold=221, count=100)

    def claim_invest_reward(self):
        """Забирает награду за расследование."""
        if self.invest_reward_appear():
            logger.info('Награда за исследование появилась')
        else:
            logger.info('Награды за исследование нет')
            return False
        # Получаем награду
        skip_first_screenshot = True
        clicked = True
        self.interval_clear(HOSIPITAL_CLUE_CHECK)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if clicked:
                if self.is_in_clue() and not self.invest_reward_appear():
                    return True
            if self.handle_get_items():
                clicked = True
                continue
            if self.is_in_clue(interval=2):
                if self.invest_reward_appear():
                    self.device.click(INVEST_REWARD_RECEIVE)
                    continue

    def loop_aside(self):
        """Обходит реплики во всех вкладках и проводит расследования."""
        while 1:
            logger.hr('Цикл реплик госпиталя', level=1)
            HOSPITAL_TAB.set('LOCATION', main=self)
            selected = self.select_aside()
            if not selected:
                break
            self.loop_invest()

        while 1:
            logger.hr('Цикл реплик госпиталя', level=1)
            HOSPITAL_TAB.set('CHARACTER', main=self)
            selected = self.select_aside()
            if not selected:
                break
            self.loop_invest()

        while 1:
            logger.hr('Цикл реплик госпиталя', level=1)
            HOSPITAL_TAB.set('CHARACTER', main=self)
            self.aside_swipe_down()
            selected = self.select_aside()
            if not selected:
                break
            self.loop_invest()

        logger.info('Цикл реплик госпиталя завершён')

    def aside_swipe_down(self, skip_first_screenshot=True):
        """Прокручивает список реплик вниз до отсутствия индикатора следующей страницы."""
        logger.info('Прокрутка реплик вниз')
        swiped = False
        interval = Timer(2, count=6)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if swiped and not self.appear(ASIDE_NEXT_PAGE, offset=(20, 20)):
                logger.info('Реплики достигли конца списка')
                break
            if interval.reached():
                p1, p2 = random_rectangle_vector(
                    vector=(0, -200), box=CLUE_LIST.area, random_range=(-20, -10, 20, 10))
                self.device.swipe(p1, p2)
                interval.reset()
                swiped = True
                continue

    def run(self):
        """Основная точка входа события больницы."""
        # Проверяем доступность события
        if self.event_time_limit_triggered():
            self.config.task_stop()
        self.ui_ensure(page_campaign_menu)
        if self.is_event_entrance_available():
            self.ui_goto(page_hospital)

        # Получаем ежедневную награду
        self.daily_reward_receive()

        # Выполняем событие
        self.clue_enter()
        try:
            self.loop_aside()
            # Scheduler
            self.config.task_delay(server_update=True)
        except OilExhausted:
            self.clue_exit()
            logger.hr('Условие остановки: лимит топлива')
            self.config.task_delay(minute=(120, 240))
        except ScriptEnd as e:
            logger.hr('Завершение скрипта')
            logger.info(str(e))
            self.clue_exit()
        except TaskEnd:
            self.clue_exit()
            raise


if __name__ == '__main__':
    self = Hospital('alas')
    self.device.screenshot()
    self.loop_aside()
