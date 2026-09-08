"""
Базовые UI-операции системы Research.

Модуль предоставляет низкоуровневые UI-операции для Research, включая:
- определение страницы: главная страница Research или страница очереди;
- ожидание стабильности страницы: завершение анимации карточек Research;
- навигацию входа на страницу очереди и выхода с неё;
- получение наградных предметов и сохранение данных о добыче;
- определение состояния проектов по шаблонам: waiting/running/detail;
- выход из карточки проекта и отмену Research.

Модуль является общей базой для ResearchSelector, ResearchQueue и RewardResearch
и предоставляет им единый интерфейс UI-операций.

Термины:
    Research Queue: область страницы Research, вмещающая 5 проектов очереди;
    Detail: страница подробностей, открывающаяся после выбора проекта;
    Get Items: экран с предметами, появляющийся после получения награды Research.
"""
from module.base.timer import Timer
from module.base.utils import crop, rgb2gray
from module.combat.assets import GET_ITEMS_1, GET_ITEMS_2, GET_ITEMS_3, GET_ITEMS_3_CHECK
from module.logger import logger
from module.research.assets import *
from module.research.project import RESEARCH_STATUS
from module.research.series import RESEARCH_SCALING
from module.ui.assets import BACK_ARROW, RESEARCH_CHECK
from module.ui.ui import UI


class ResearchUI(UI):
    """
    Базовые UI-операции Research.

    Все операции UI, связанные с Research — определение страницы, ожидание
    стабильности, навигация по очереди, получение наград и определение состояния —
    собраны в этом классе для использования составными модулями верхнего уровня.

    Наследуется от UI и получает возможности навигации по страницам и общие
    UI-операции.
    """
    def is_in_research(self, interval=0):
        """
        Проверяет, открыта ли главная страница Research (список проектов).

        Аргументы:
            interval (int): интервал проверки кнопки; 0 означает проверять каждый раз.

        Результат:
            bool: открыта ли главная страница Research.
        """
        return self.appear(RESEARCH_CHECK, offset=(20, 20), interval=interval)

    def is_in_queue(self, interval=0):
        """
        Проверяет, открыта ли страница очереди Research.

        Аргументы:
            interval (int): интервал проверки кнопки; 0 означает проверять каждый раз.

        Результат:
            bool: открыта ли страница очереди Research.
        """
        return self.appear(QUEUE_CHECK, offset=(20, 20), interval=interval)

    def ensure_research_stable(self):
        """
        Ожидает завершения анимации списка проектов Research.

        Последующие операции выполняются только после завершения анимации
        переключения или загрузки карточек, чтобы избежать ложного распознавания.
        """
        self.wait_until_stable(STABLE_CHECKER)

    def ensure_research_center_stable(self):
        """
        Ожидает завершения анимации центральной области списка Research.

        Подобно ensure_research_stable, но использует проверку центральной области
        и подходит, например, для возврата со страницы очереди.
        """
        self.wait_until_stable(STABLE_CHECKER_CENTER)

    def queue_enter(self, skip_first_screenshot=True):
        """
        Страницы:
            in: is_in_research
            out: is_in_queue
        """
        self.ui_click(RESEARCH_GOTO_QUEUE, check_button=self.is_in_queue, appear_button=self.is_in_research,
                      retry_wait=1, skip_first_screenshot=skip_first_screenshot)

    def queue_quit(self):
        """
        Страницы:
            in: is_in_queue
            out: is_in_research, стабильный список проектов
        """
        logger.info('[Исследование — очередь] Выход из очереди')
        for _ in self.loop():
            if self.is_in_research():
                break
            if self.is_in_queue(interval=3):
                self.device.click(BACK_ARROW)
                continue
            # Обрабатываем get_items.
            # Обычно get_items обрабатывается во время получения награды, но сеть
            # иногда отвечает с задержкой.
            if self.appear(GET_ITEMS_1, offset=(20, 20), interval=3):
                logger.info(f'[Исследование — очередь] {GET_ITEMS_1} -> {GET_ITEMS_RESEARCH_SAVE}')
                self.device.click(GET_ITEMS_RESEARCH_SAVE)
                continue
            if self.appear(GET_ITEMS_2, offset=(20, 20), interval=3):
                logger.info(f'[Исследование — очередь] {GET_ITEMS_1} -> {GET_ITEMS_RESEARCH_SAVE}')
                self.device.click(GET_ITEMS_RESEARCH_SAVE)
                continue

        self.ensure_research_center_stable()

    def get_items(self):
        """Возвращает кнопку текущего окна получения предметов или None."""
        if self.appear(GET_ITEMS_3, offset=(5, 5)):
            if self.image_color_count(GET_ITEMS_3_CHECK, color=(255, 255, 255), threshold=221, count=100):
                return GET_ITEMS_3
            else:
                return GET_ITEMS_2
        if self.appear(GET_ITEMS_2, offset=(5, 5)):
            return GET_ITEMS_2
        if self.appear(GET_ITEMS_1, offset=(5, 5)):
            return GET_ITEMS_1
        return None

    def drop_record(self, drop, known_button=None):
        """
        Аргументы:
            drop (DropRecord):
            known_button (Button | None): Уже подтверждённый тип окна награды.
        """
        if not drop:
            return
        button = known_button if known_button is not None else self.get_items()
        if button == GET_ITEMS_1 or button == GET_ITEMS_2:
            drop.add(self.device.image)
        elif button == GET_ITEMS_3:
            self.device.sleep(1.5)
            self.device.screenshot()
            drop.add(self.device.image)
            self.device.swipe_vector((0, 250), box=ITEMS_3_SWIPE.area, random_range=(-10, -10, 10, 10),
                                     padding=0)
            self.device.sleep(2)
            self.device.screenshot()
            drop.add(self.device.image)

    def get_research_status(self, image):
        """
        Аргументы:
            image: скриншот.

        Результат:
            list[str]: список состояний проектов.
        """
        out = []
        for index, status, scaling in zip(range(5), RESEARCH_STATUS, RESEARCH_SCALING):
            info = status.crop((0, -40, 200, 0))
            piece = rgb2gray(crop(image, info.area, copy=False))
            if TEMPLATE_WAITING.match(piece, scaling=scaling, similarity=0.75):
                out.append('waiting')
            elif TEMPLATE_RUNNING.match(piece, scaling=scaling, similarity=0.75):
                out.append('running')
            elif TEMPLATE_DETAIL.match(piece, scaling=scaling, similarity=0.75):
                out.append('detail')
            else:
                out.append('unknown')

        logger.debug(f'[Исследование — состояние] Состояние исследования: {out}')
        return out

    def is_research_stabled(self):
        """
        Проверяет, стабильна ли главная страница Research без анимации.

        Страница считается загруженной, если на ней есть проект в состоянии
        'detail'.

        Результат:
            bool: стабильна ли главная страница Research.
        """
        return self.is_in_research() and 'detail' in self.get_research_status(self.device.image)

    def research_detail_quit(self, skip_first_screenshot=True):
        """
        Возвращается со страницы деталей проекта на главную страницу Research.

        Нажимает кнопку выхода и ждёт стабильного списка проектов. Выполняющийся
        проект при этом не отменяется.

        Аргументы:
            skip_first_screenshot (bool): пропустить ли первый скриншот.
        """
        logger.info('[Исследование — детали] Выход из деталей проекта')
        click_timer = Timer(10)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.is_research_stabled():
                break

            if self.appear(RESEARCH_UNAVAILABLE, offset=(20, 20)) \
                    or self.appear(RESEARCH_START, offset=(20, 20)) \
                    or self.appear(RESEARCH_STOP, offset=(20, 20)):
                if click_timer.reached():
                    self.device.click(RESEARCH_DETAIL_QUIT)
                    click_timer.reset()

    def research_detail_cancel(self, skip_first_screenshot=True):
        """
        Отменяет выполняющийся проект Research и возвращается на главную страницу.

        Нажимает кнопку остановки, подтверждает диалог и ждёт стабильного списка
        проектов. В отличие от research_detail_quit, этот метод отменяет Research.

        Аргументы:
            skip_first_screenshot (bool): пропустить ли первый скриншот.
        """
        logger.info('[Исследование — детали] Отмена проекта')
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.is_research_stabled():
                break

            if self.appear_then_click(RESEARCH_STOP, offset=(20, 20), interval=5):
                continue
            if self.handle_popup_confirm('RESEARCH_CANCEL'):
                continue
            if self.appear(RESEARCH_START, offset=(20, 20), interval=5):
                self.device.click(RESEARCH_DETAIL_QUIT)
                continue
