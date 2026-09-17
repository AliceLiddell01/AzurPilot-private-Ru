"""Модуль элементов управления переключателями в игре. Определяет класс Switch, инкапсулирующий
логику переключения состояний переключателей/селекторов с поддержкой механизма повторов."""

import logging

from module.base.base import ModuleBase
from module.base.timer import Timer
from module.exception import ScriptError
from module.logger import logger


class Switch:
    """
    Обертка элемента управления переключателем в игре; поддерживает переключение между несколькими состояниями с механизмом повторов.

    Examples:
        # Определение
        submarine_hunt = Switch('Submarine_hunt', offset=120)
        submarine_hunt.add_state('on', check_button=SUBMARINE_HUNT_ON)
        submarine_hunt.add_state('off', check_button=SUBMARINE_HUNT_OFF)

        # Переключение в состояние ON
        submarine_view.set('on', main=self)
    """

    def __init__(self, name='Switch', is_selector=False, offset=0):
        """
        Args:
            name (str): Имя переключателя.
            is_selector (bool): True означает селектор с несколькими вариантами выбора кликом.
                Например: | [Ежедневные] | Срочные | -> клик -> | Ежедневные | [Срочные] |
                False означает двухпозиционный переключатель, состояние которого меняется кликом в одну область.
                Например: | [Вкл] | -> клик -> | [Выкл] |
        """
        self.name = name
        self.is_selector = is_selector
        self._offset = offset
        self.state_list = []
        self.set_unknown_timer = Timer(5, count=10)
        self.set_click_timer = Timer(1, count=2)
        self.wait_timeout = Timer(2, count=4)

    def add_state(self, state, check_button, click_button=None, offset=0, similarity=0.85):
        """
        Добавить доступное для переключения состояние.

        Args:
            state (str): Имя состояния; нельзя использовать 'unknown'.
            check_button (Button): Кнопка для проверки этого состояния.
            click_button (Button): Кнопка для клика переключения в это состояние; по умолчанию совпадает с check_button.
            offset (bool, int, tuple): Смещение сопоставления.
            similarity (float): Порог схожести шаблона при использовании смещения.
        """
        if state == 'unknown':
            raise ScriptError(f'Нельзя использовать "unknown" как имя состояния')
        self.state_list.append({
            'state': state,
            'check_button': check_button,
            'click_button': click_button if click_button is not None else check_button,
            'offset': offset if offset else self._offset,
            'similarity': similarity,
        })

    @property
    def offset(self):
        return self._offset

    @offset.setter
    def offset(self, value):
        self._offset = value
        for data in self.state_list:
            data['offset'] = value

    def appear(self, main):
        """
        Проверить, отображается ли переключатель на экране (то есть состояние не 'unknown').

        Args:
            main (ModuleBase): Экземпляр базового модуля.

        Returns:
            bool: Отображается ли переключатель.
        """
        return self.get(main=main) != 'unknown'

    def get(self, main):
        """
        Получить текущее состояние переключателя.

        Args:
            main (ModuleBase): Экземпляр базового модуля.

        Returns:
            str: Имя состояния или 'unknown'.
        """
        for data in self.state_list:
            if main.appear(data['check_button'], offset=data['offset'], similarity=data['similarity']):
                return data['state']

        return 'unknown'

    def click(self, state, main):
        """
        Кликнуть по кнопке, соответствующей указанному состоянию.

        Args:
            state (str): Имя целевого состояния.
            main (ModuleBase): Экземпляр базового модуля.
        """
        button = self.get_data(state)['click_button']
        main.device.click(button)

    def get_data(self, state):
        """
        Получить данные указанного состояния.

        Args:
            state (str): Имя состояния.

        Returns:
            dict: Данные состояния, добавленные в add_state.

        Raises:
            ScriptError: Если состояние недопустимо.
        """
        for row in self.state_list:
            if row['state'] == state:
                return row

        raise ScriptError(f'Переключатель {self.name} получил недопустимое состояние: {state}')

    def handle_additional(self, main):
        """
        Обработать дополнительные всплывающие окна; подклассы могут переопределять этот метод.

        Args:
            main (ModuleBase): Экземпляр базового модуля.

        Returns:
            bool: Было ли обработано всплывающее окно.
        """
        return False

    def set(self, state, main, skip_first_screenshot=True):
        """
        Установить переключатель в указанное состояние с механизмом повторов и тайм-аута.

        Args:
            state: Имя целевого состояния.
            main (ModuleBase): Экземпляр базового модуля.
            skip_first_screenshot (bool): Пропускать ли первый снимок экрана.

        Returns:
            bool: Был ли выполнен клик.
        """
        logger.info(f'{self.name}: установка состояния {state}')
        self.get_data(state)

        log_key = ('switch-state', id(self), 'set')
        logger.reset_suppression(log_key)
        changed = False
        has_unknown = False
        unknown_timer = self.set_unknown_timer.reset()
        click_timer = self.set_click_timer.clear()
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                main.device.screenshot()

            # Определяем текущее состояние
            current = self.get(main=main)
            logger.log_suppressed(
                logging.DEBUG,
                f'[UI — Переключатель] {self.name}: состояние {current}',
                key=log_key,
                payload=current,
            )

            # Выходим после достижения целевого состояния
            if current == state:
                logger.finish_suppressed(log_key)
                return changed

            # Обрабатываем дополнительные окна
            if self.handle_additional(main=main):
                continue

            # Предупреждение о неизвестном состоянии
            if current == 'unknown':
                if unknown_timer.reached():
                    logger.warning(f'[UI — Переключатель] Состояние переключателя {self.name} не распознано; '
                                   f'ресурсы следует перепроверить')
                    has_unknown = True
                    unknown_timer.reset()
                # Пока unknown_timer ни разу не сработал, не кликаем неизвестное состояние: это может быть анимация переключения.
                # Если unknown_timer уже срабатывал, игнорируем неизвестное состояние и кликаем целевое
                # — это может быть ещё не добавленное новое состояние.
                # Благодаря игнорированию нового состояния Switch.set() всё ещё может переключаться между известными состояниями.
                if not has_unknown:
                    continue
            else:
                # Состояние известно — сбрасываем таймер
                unknown_timer.reset()

            # Выполняем переключение
            if click_timer.reached():
                if self.is_selector:
                    # Режим селектора: кликаем целевое состояние
                    click_state = state
                else:
                    # Режим переключателя: кликаем текущее состояние, чтобы перейти в другое
                    # Но 'unknown' кликнуть нельзя, поэтому в этом случае кликаем целевое состояние
                    # Предполагается, что все состояния селектора используют одну позицию
                    if current == 'unknown':
                        click_state = state
                    else:
                        click_state = current
                self.click(click_state, main=main)
                changed = True
                click_timer.reset()
                unknown_timer.reset()

        return changed

    def wait(self, main, skip_first_screenshot=True):
        """
        Ожидать активации любого состояния.

        Args:
            main (ModuleBase): Экземпляр базового модуля.
            skip_first_screenshot: Пропускать ли первый снимок экрана.

        Returns:
            bool: Успешно ли обнаружено состояние.
        """
        log_key = ('switch-state', id(self), 'wait')
        logger.reset_suppression(log_key)
        timeout = self.wait_timeout.reset()
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                main.device.screenshot()

            # Определяем текущее состояние
            current = self.get(main=main)
            logger.log_suppressed(
                logging.DEBUG,
                f'[UI — Переключатель] {self.name}: состояние {current}',
                key=log_key,
                payload=current,
            )

            # Выходим, когда обнаружено известное состояние
            if current != 'unknown':
                logger.finish_suppressed(log_key)
                return True
            if timeout.reached():
                logger.finish_suppressed(log_key)
                logger.warning(f'{self.name}: превышено время ожидания активации')
                return False

            # Обрабатываем дополнительные окна
            if self.handle_additional(main=main):
                continue
