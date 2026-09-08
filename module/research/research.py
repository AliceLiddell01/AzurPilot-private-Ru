"""
Основной обработчик задачи Research.

Модуль реализует полный автоматизированный процесс Research, включая:
- поиск завершённых проектов и получение наград;
- выбор оптимального проекта по правилам фильтрации из конфигурации;
- запуск проекта и добавление его в очередь Research;
- заполнение очереди (не более 5 слотов очереди и 1 проекта вне очереди);
- обработку специальных типов проектов (для серии E нужно разобрать снаряжение,
  для серии T — выполнить поручения);
- отложенный запуск Research: при нехватке ресурсов очередь расходуется естественным
  образом.

Основной класс RewardResearch наследуется от ResearchSelector (обнаружение и выбор
проектов), ResearchQueue (управление очередью) и StorageHandler (разбор снаряжения)
и является точкой входа задачи research в AzurLaneAutoScript.

Термины:
    Research Queue: очередь не более чем из 5 проектов;
    проект вне очереди: дополнительный проект, выполняющийся напрямую;
    принудительный режим (enforce): ослабление фильтра, если обычный результат пуст.
"""
from datetime import timedelta

import numpy as np

from module.base.timer import Timer
from module.base.utils import rgb2gray
from module.config.time_source import now as current_time
from module.exception import (
    ResearchProjectStartError,
    ResearchRewardPopupTimeoutError,
    ResearchRewardReturnTimeoutError,
)
from module.logger import logger
from module.ocr.ocr import Duration
from module.research.assets import *
from module.research.project import RESEARCH_STATUS, get_research_finished
from module.research.rqueue import ResearchQueue
from module.research.selector import RESEARCH_ENTRANCE, ResearchSelector
from module.storage.storage import StorageHandler
from module.ui.assets import RESEARCH_CHECK
from module.ui.page import page_research

OCR_DURATION = Duration(RESEARCH_LAB_DURATION_REMAIN, letter=(255, 255, 255), threshold=64,
                        name='RESEARCH_LAB_DURATION_REMAIN')

_RESEARCH_REWARD_POPUP_STABILIZATION_SECONDS = 1.5
_RESEARCH_REWARD_POPUP_STABILIZATION_COUNT = 5
_RESEARCH_REWARD_POPUP_TIMEOUT_SECONDS = 15
_RESEARCH_REWARD_POPUP_TIMEOUT_COUNT = 30
_RESEARCH_REWARD_RETURN_CONFIRM_SECONDS = 0.5
_RESEARCH_REWARD_RETURN_CONFIRM_COUNT = 2
_RESEARCH_REWARD_RETURN_TIMEOUT_SECONDS = 15
_RESEARCH_REWARD_RETURN_TIMEOUT_COUNT = 30


class RewardResearch(ResearchSelector, ResearchQueue, StorageHandler):
    """
    Основной обработчик задачи Research, управляющий полным жизненным циклом
    проектов Research.

    Множественное наследование объединяет следующие возможности:
    - ResearchSelector: обнаружение, фильтрация и сортировка проектов;
    - ResearchQueue: добавление проектов в очередь, определение состояния и получение
      наград;
    - StorageHandler: разбор снаряжения как предварительное условие серии E.

    Обзор процесса Research:
    1. перейти на страницу Research;
    2. войти в очередь и получить награды завершённых проектов;
    3. обработать отложенный проект серии T, требующий поручений;
    4. получить награду проекта вне очереди;
    5. заполнять очередь, пока не будут заняты 5 слотов;
    6. рассчитать время следующего запуска.

    Атрибуты:
        _research_project_offset (int): смещение списка проектов на экране,
            используемое для поправки индекса при нажатии на проект не в центре;
        _research_finished_index (int): индекс завершённого проекта (0-4),
            используемый для определения его позиции на экране;
        research_project_started (ResearchProject): объект последнего успешно
            запущенного проекта Research или None, если проект не запущен;
        enforce (bool): включён ли принудительный режим. Если обычный результат
            фильтра пуст, режим автоматически ослабляет условия выбора;
        end_time (datetime): ожидаемое время завершения первого проекта очереди,
            используемое для расчёта задержки следующего запуска.
    """
    _research_project_offset = 0
    _research_finished_index = 2
    research_project_started = None  # Объект ResearchProject
    enforce = False
    end_time = None

    def research_has_finished(self):
        """
        Проверяет, есть ли завершённый проект Research.

        Завершённый проект обычно автоматически перемещается в центр, но иногда
        из-за неизвестной ошибки игры этого не происходит.

        Результат:
            bool: есть ли завершённый проект Research.
        """
        index = get_research_finished(self.device.image)
        if index is not None:
            logger.attr('Исследование завершено', index)
            self._research_finished_index = index
            return True
        else:
            return False

    def research_reset(self, drop=None, skip_first_screenshot=True):
        """
        Сбрасывает список проектов Research и обновляет доступные проекты.

        Выполняет сброс только при доступной функции сброса (видна кнопка
        RESET_AVAILABLE). После сброса список проектов обновляется, а прежний
        результат фильтра становится недействительным.

        Аргументы:
            drop (DropImage): объект записи добычи для сохранения скриншота до сброса.
            skip_first_screenshot (bool): пропустить ли первый скриншот.

        Результат:
            bool: выполнен ли сброс; False, если функция сброса недоступна.
        """
        if not self.appear(RESET_AVAILABLE, threshold=10):
            logger.info('[Исследование — сброс] Сброс исследований недоступен')
            return False

        logger.info('[Исследование — сброс] Сброс исследований')
        drop.add(self.device.image)
        executed = False
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear_then_click(RESET_AVAILABLE, interval=10, threshold=10):
                continue
            if self.handle_popup_confirm('RESEARCH_RESET'):
                executed = True
                continue

            # Условие завершения.
            if executed and self.is_in_research():
                self.ensure_no_info_bar(timeout=3)  # Сброс выполнен.
                self.ensure_research_stable()
                break

        self._research_project_offset = 0
        return True

    def research_enforce(self, drop=None, add_queue=True):
        """
        Принудительно выбирает проект Research, игнорируя часть фильтров.

        Если обычный результат фильтра пуст, включает принудительный режим и повторно
        выбирает проект по более мягким правилам. Это гарантирует, что проект будет
        выполняться.

        Аргументы:
            drop (DropImage): объект записи добычи.
            add_queue (bool): добавить ли проект в очередь.
                Проект вне очереди нельзя добавить в очередь, поэтому нужен этот флаг.

        Результат:
            bool: выбран ли проект.
        """
        if not self.enforce:
            logger.info('[Исследование — принудительно] Принудительный выбор проекта')
            self.enforce = True
            return self.research_select(self.research_sort_filter(self.enforce),
                                        drop=drop, add_queue=add_queue)
        return True

    def research_select(self, priority, drop=None, add_queue=True):
        """
        Аргументы:
            priority (list): список объектов ResearchProject и управляющих строк,
                например [object, object, object, 'reset'];
            drop (DropImage): объект записи добычи.
            add_queue (bool): добавить ли проект в очередь.
                Проект вне очереди нельзя добавить в очередь, поэтому нужен этот флаг.

        Результат:
            bool: False, если был выполнен сброс.
        """
        if not len(priority):
            logger.info('[Исследование — выбор] Нет проектов, соответствующих текущему фильтру')
            return self.research_enforce(drop=drop, add_queue=add_queue)
        for project in priority:
            # Пример приоритета: ['reset', 'shortest'].
            if project == 'reset':
                if self.research_reset(drop=drop):
                    return False
                else:
                    continue

            if isinstance(project, str):
                # Пример приоритета: ['shortest'].
                if project == 'shortest':
                    self.research_select(self.research_sort_shortest(self.enforce),
                                         drop=drop, add_queue=add_queue)
                elif project == 'cheapest':
                    self.research_select(self.research_sort_cheapest(self.enforce),
                                         drop=drop, add_queue=add_queue)
                else:
                    logger.warning(f'[Исследование — выбор] Неизвестный метод выбора: {project}')
                return True
            elif project.genre.upper() in ['C', 'T'] and not self.enforce:
                return self.research_enforce(drop=drop, add_queue=add_queue)
            else:
                # Пример приоритета: [ResearchProject, ResearchProject].
                ret = self.research_project_start_with_requirements(project, add_queue=add_queue)
                if ret:
                    return True
                elif ret is not None and self.config.Research_RemainingCommissions > 0:
                    logger.info('[Исследование — задержка] Исследование отложено из-за проекта типа T')
                    return True
                elif ret is not None and self.research_delay_check():
                    logger.info('[Исследование — задержка] Недостаточно ресурсов, а очередь не пуста; исследование отложено')
                    return True
                else:
                    continue

        logger.info('[Исследование — выбор] Исследовательский проект не запущен')
        return self.research_enforce(drop=drop, add_queue=add_queue)

    def research_delay_check(self):
        """
        Проверяет, разрешён ли отложенный запуск Research.

        Результат:
            bool: разрешён ли отложенный запуск Research.
        """
        if self.config.Research_AllowDelay:
            slot = self.get_queue_slot()
            if slot < 4:
                return True
            if slot == 4:
                if self.end_time <= current_time():
                    return True
                elif self.end_time + timedelta(minutes=-10) > current_time():
                    return True

        return False

    def research_project_start(self, project, add_queue=True, skip_first_screenshot=True):
        """
        Запускает указанный проект и добавляет его в очередь Research.

        Аргументы:
            project (ResearchProject, int): объект проекта или индекс проекта (от 0 до 4).
            add_queue (bool): добавить ли проект в очередь.
                Проект вне очереди нельзя добавить в очередь, поэтому нужен этот флаг.
            skip_first_screenshot (bool): пропустить ли первый скриншот.

        Результат:
            bool: успешно ли запущен проект.
            None: проект для запуска отсутствует в известном списке.

        Страницы:
            in: is_in_research
            out: is_in_research
        """
        logger.hr('Запуск исследовательского проекта', level=2)
        logger.info(f'[Исследование — запуск] Проект: {project}')
        if isinstance(project, int):
            index = project
        elif project in self.projects:
            index = self.projects.index(project)
        else:
            logger.warning(f'[Исследование — запуск] Проект для запуска {project} отсутствует в списке известных проектов')
            return None
        logger.info(f'[Исследование — запуск] Индекс проекта: {index}')
        self.interval_clear([RESEARCH_START])
        self.popup_interval_clear()
        available = False
        click_timer = Timer(10)
        click_count = 0
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            max_rgb = np.max(rgb2gray(self.image_crop(RESEARCH_UNAVAILABLE, copy=False)))

            # Здесь interval не используется: RESEARCH_CHECK уже была видна 5 секунд назад.
            if click_timer.reached() and self.is_in_research():
                i = (index - self._research_project_offset) % 5
                logger.info(f'[Исследование — запуск] Смещение проекта: {self._research_project_offset}, проект {index} находится в {i}')
                self.device.click(RESEARCH_ENTRANCE[i])
                self.ensure_research_stable()
                click_count += 1
                click_timer.reset()
                continue
            if max_rgb > 235 and self.appear_then_click(RESEARCH_START, offset=(5, 20), interval=10):
                available = True
                continue
            if self.handle_popup_confirm('RESEARCH_START'):
                continue

            # Условие завершения.
            if click_count >= 3:
                logger.error('[Исследование — запуск] Не удалось запустить проект после 3 попыток; '
                             'возможно, уже выполняется проект с невыполненными условиями '
                             'или исследование завершено')
                raise ResearchProjectStartError(
                    '[Исследование — запуск] Не удалось запустить проект после 3 попыток; '
                    'возможно, условия проекта не выполнены или исследование уже завершено'
                )
            if self.appear(RESEARCH_STOP, offset=(20, 20)):
                # RESEARCH_STOP — полупрозрачная кнопка, её цвет зависит от фона.
                if add_queue:
                    if not self.research_queue_add():
                        self.research_project_started = None
                        self._research_project_offset = (index - 2) % 5
                        return False
                else:
                    self.research_detail_quit()
                # self.ensure_no_info_bar(timeout=3)  # Research запущен.
                self.research_project_started = project
                self._research_project_offset = (index - 2) % 5
                return True
            if not available and max_rgb <= 235 \
                    and self.appear(RESEARCH_UNAVAILABLE, offset=(5, 20)):
                logger.info('[Исследование — запуск] Недостаточно ресурсов для запуска проекта')
                self.research_detail_quit()
                self.research_project_started = None
                self._research_project_offset = (index - 2) % 5
                return False

    def research_project_start_with_requirements(self, project, add_queue=True):
        """
        Запускает указанный проект, добавляет его в очередь и обрабатывает
        необходимые предварительные условия.

        Аргументы:
            project (ResearchProject, int): объект проекта или индекс проекта (от 0 до 4).
            add_queue (bool): добавить ли проект в очередь.
                Проект вне очереди нельзя добавить в очередь, поэтому нужен этот флаг.

        Результат:
            bool: успешно ли запущен проект.
            None: проект для запуска отсутствует в известном списке.

        Страницы:
            in: is_in_research
            out: is_in_research
        """
        # Для индекса проекта сразу вызываем основной метод.
        if isinstance(project, int):
            return self.research_project_start(project, add_queue=add_queue)
        elif project.genre == 'E' and project.equipment_amount > 0:
            logger.info(f'[Исследование — серия E] Подготовка к запуску проекта {project} '
                        f'с разбором оборудования: {project.equipment_amount}')
            # Запуск проекта.
            self.research_project_start(project, add_queue=False)
            # Разбор снаряжения.
            self.storage_disassemble_equipment(amount=project.equipment_amount)
            # Возврат на страницу Research.
            self.ui_ensure(page_research)
            self.research_project_list_init()
            # Добавление в очередь.
            result = self.research_project_start(project, add_queue=add_queue)
            if result is None:
                logger.error('[Исследование — серия E] После разбора оборудования исследовательский проект исчез')
            return result
        elif project.genre == 'T':
            logger.info(f'[Исследование — серия T] Подготовка к запуску проекта: {project}')
            self.research_project_start(project, add_queue=False)
            self.config.Research_RemainingCommissions = project.commission_amount
            self.research_project_started = None
            return False
        else:
            # Обычный проект.
            return self.research_project_start(project, add_queue=add_queue)

    def research_receive(self, skip_first_screenshot=True):
        """
        Получает награду завершённого проекта на главной странице Research.

        Находит завершённый проект, открывает его и получает наградные предметы,
        поддерживая запись добычи. Если время проекта истекло, но условия не
        выполнены, проект пропускается.

        Аргументы:
            skip_first_screenshot (bool): пропустить ли первый скриншот.

        Страницы:
            in: page_research, стабильная страница с завершённым проектом.
            out: page_research

        Результат:
            bool: True, если награда получена; False, если условия проекта
                  не выполнены.
        """
        logger.hr('Получение награды за исследование', level=3)
        with self.stat.new(
                genre='research', method=self.config.DropRecord_ResearchRecord
        ) as record:
            # Сохраняем скриншот списка проектов.
            record.add(self.device.image)

            # После входа в завершённый проект окно награды становится владельцем
            # цикла до явного сохранения результата.
            popup_timeout = Timer(
                _RESEARCH_REWARD_POPUP_TIMEOUT_SECONDS,
                count=_RESEARCH_REWARD_POPUP_TIMEOUT_COUNT,
            ).start()
            popup_confirm = Timer(
                _RESEARCH_REWARD_POPUP_STABILIZATION_SECONDS,
                count=_RESEARCH_REWARD_POPUP_STABILIZATION_COUNT,
            )
            reward_popup_pending = False
            record_button = None
            while 1:
                if skip_first_screenshot:
                    skip_first_screenshot = False
                else:
                    self.device.screenshot()

                if reward_popup_pending:
                    appear_button = self.get_items()
                    if appear_button is not None and appear_button != record_button:
                        logger.info(f'[Исследование — получение] Появился {appear_button}')
                        record_button = appear_button
                        popup_confirm.reset()

                    if popup_confirm.reached():
                        break
                    if popup_timeout.reached():
                        raise ResearchRewardPopupTimeoutError(
                            '[Исследование — награда] Не удалось стабилизировать окно награды '
                            f'за {_RESEARCH_REWARD_POPUP_TIMEOUT_SECONDS} с; '
                            f'фаза=стабилизация, layout={record_button}'
                        )
                    continue

                if popup_timeout.reached():
                    raise ResearchRewardPopupTimeoutError(
                        '[Исследование — награда] Окно награды не было обнаружено '
                        f'за {_RESEARCH_REWARD_POPUP_TIMEOUT_SECONDS} с; '
                        'фаза=обнаружение, ожидается GET_ITEMS_*'
                    )

                # В первую очередь проверяем модальное окно, чтобы не обслуживать
                # затемнённый экран Research в том же кадре.
                appear_button = self.get_items()
                if appear_button is not None:
                    logger.info(f'[Исследование — получение] Появился {appear_button}')
                    reward_popup_pending = True
                    record_button = appear_button
                    popup_confirm.reset()
                    continue

                if self.appear(RESEARCH_CHECK, offset=(20, 20), interval=10):
                    if self.research_has_finished():
                        self.device.click(RESEARCH_ENTRANCE[self._research_finished_index])
                        continue

                if self.appear(RESEARCH_STOP, offset=(20, 20)):
                    logger.info('[Исследование — получение] Время исследования истекло, но условия не выполнены')
                    self.research_project_started = None
                    self.research_detail_quit()
                    return False
                # Открыт другой проект.
                if self.appear(RESEARCH_START, offset=(20, 20), interval=5):
                    self.device.click(RESEARCH_DETAIL_QUIT)
                    continue

            # Сохраняем наградные предметы.
            self.drop_record(drop=record, known_button=record_button)

        # Явно закрываем окно награды и отдельно подтверждаем возврат на стабильный
        # экран Research. Один RESEARCH_CHECK под модальным окном успехом не считается.
        self.device.click(GET_ITEMS_RESEARCH_SAVE)
        return_timeout = Timer(
            _RESEARCH_REWARD_RETURN_TIMEOUT_SECONDS,
            count=_RESEARCH_REWARD_RETURN_TIMEOUT_COUNT,
        ).start()
        return_confirm = Timer(
            _RESEARCH_REWARD_RETURN_CONFIRM_SECONDS,
            count=_RESEARCH_REWARD_RETURN_CONFIRM_COUNT,
        ).reset()
        last_research_status = None
        while 1:
            self.device.screenshot()
            popup_button = self.get_items()
            if popup_button is None and self.is_in_research():
                last_research_status = self.get_research_status(self.device.image)
                # `is_research_stabled()` используется навигацией и считает
                # страницу готовой уже при одном `detail`. После SAVE этого
                # недостаточно: `get_items()` может временно пропустить ещё
                # открытый popup, а карточки Research могут быть в переходе.
                # `get_research_status()` возвращает по одному состоянию для
                # каждого слота; отсутствие `unknown` — существующий барьер,
                # который также используется перед продолжением сценария обработки
                # проекта вне очереди. Два подтверждения подряд относятся к свежим кадрам.
                if (
                        isinstance(last_research_status, list)
                        and len(last_research_status) == len(RESEARCH_STATUS)
                        and 'unknown' not in last_research_status
                ):
                    if return_confirm.reached():
                        return True
                else:
                    return_confirm.reset()
            else:
                return_confirm.reset()

            if return_timeout.reached():
                raise ResearchRewardReturnTimeoutError(
                    '[Исследование — награда] После SAVE не подтверждён устойчивый экран Research '
                    f'за {_RESEARCH_REWARD_RETURN_TIMEOUT_SECONDS} с; '
                    f'фаза=возврат, layout={record_button}, '
                    f'состояние={last_research_status}'
                )

    def queue_receive(self, skip_first_screenshot=True):
        """
        Получает все завершённые награды из очереди исследований.

        Окно `GET_ITEMS_*` имеет приоритет над определением конца очереди:
        после первого положительного распознавания оно считается активным до
        явного нажатия `GET_ITEMS_RESEARCH_SAVE`. Это не позволяет более
        короткому таймеру окончания очереди оборвать обработку награды.

        Аргументы:
            skip_first_screenshot (bool): Пропустить ли первый снимок экрана.

        Страницы:
            in: is_in_queue
            out: is_in_queue

        Результат:
            int: Количество полученных наград исследовательских проектов.
        """
        logger.hr('Получение наград очереди', level=1)
        total = 0
        with self.stat.new(
                genre='research', method=self.config.DropRecord_ResearchRecord
        ) as drop:
            # Сохраняем исходный экран очереди для статистики наград.
            drop.add(self.device.image)

            end_confirm = Timer(1, count=3)
            item_confirm = Timer(1.5, count=5)
            item_interval = Timer(0.2, count=0)
            record_button = None
            while 1:
                if skip_first_screenshot:
                    skip_first_screenshot = False
                else:
                    self.device.screenshot()

                # Сначала обслуживаем уже обнаруженное окно награды. Пока оно
                # ожидает подтверждения, конец очереди проверять нельзя.
                if drop:
                    if record_button is None:
                        appear_button = self.get_items()
                        if appear_button is not None:
                            logger.info(f'[Исследование — получение] Появился {appear_button}')
                            record_button = appear_button
                            item_confirm.reset()

                    if record_button is not None:
                        end_confirm.reset()
                        if item_confirm.reached():
                            self.drop_record(drop=drop, known_button=record_button)
                            self.device.click(GET_ITEMS_RESEARCH_SAVE)
                            item_confirm.reset()
                            record_button = None
                            total += 1
                        continue
                else:
                    # Без записи дропа окно награды закрывается сразу после обнаружения.
                    if item_interval.reached():
                        appear_button = self.get_items()
                        if appear_button is not None:
                            self.device.click(GET_ITEMS_RESEARCH_SAVE)
                            item_interval.reset()
                            end_confirm.reset()
                            total += 1
                            continue

                # Только когда окна награды нет, можно подтверждать конец очереди.
                if self.is_in_queue() and not self.appear(QUEUE_CLAIM_REWARD, offset=None):
                    if end_confirm.reached():
                        break
                else:
                    end_confirm.reset()

                # Получаем следующую готовую награду и обязательно запускаем
                # подтверждение конца очереди заново после клика.
                if self.appear_then_click(QUEUE_CLAIM_REWARD, offset=None, interval=5):
                    end_confirm.reset()
                    continue

            if total <= 0:
                drop.clear()

        logger.info(f'[Исследование — очередь] Получены награды из проектов: {total}')
        return total

    def queue_quit(self, *args, **kwargs):
        super().queue_quit(*args, **kwargs)
        self._research_project_offset = 0

    def research_project_list_init(self, from_queue=False):
        """
        Подготавливает список проектов Research: сбрасывает смещение и распознаёт проекты.

        Аргументы:
            from_queue (bool): выполняется ли переход со страницы очереди; в этом
                случае ensure_research_center_stable() уже был вызван.
        """
        self._research_project_offset = 0
        # Обрабатываем информационную панель и делаем дополнительный скриншот,
        # чтобы дождаться исчезновения остатка info_bar.
        if self.handle_info_bar():
            self.device.screenshot()
        if not from_queue:
            self.ensure_research_center_stable()
        self.research_detect()

    def research_queue_append(self, drop=None, add_queue=True):
        """
        Выбирает и запускает проект Research из списка.

        Инициализирует список, выполняет фильтрацию и выбор, делая не более двух
        попыток. После успешного запуска сохраняет объект запущенного проекта.

        Аргументы:
            drop (DropImage): объект записи добычи.
            add_queue (bool): добавить ли проект в очередь.
                Проект вне очереди нельзя добавить в очередь, поэтому нужен этот флаг.

        Результат:
            bool: успешно ли запущен проект.
        """
        self.research_project_started = None
        project_record = None
        for _ in range(2):
            logger.hr('Выбор исследовательского проекта', level=2)
            self.research_project_list_init(from_queue=True)
            project_record = self.device.image
            priority = self.research_sort_filter()
            result = self.research_select(priority, drop=drop, add_queue=add_queue)
            if result:
                break

        if self.research_project_started is not None:
            if project_record is not None:
                drop.add(project_record)
            return True
        else:
            return False

    def research_fill_queue(self):
        """
        Выбирает проекты Research, пока очередь не заполнится.

        Результат:
            int: количество проектов Research, добавленных в очередь.

        Страницы:
            in: is_in_research
        """
        logger.hr('Заполнение очереди исследований', level=1)
        total = 0
        with self.stat.new(
                genre='research', method=self.config.DropRecord_ResearchRecord
        ) as drop:
            for _ in range(5):
                if self.get_queue_slot() > 0:
                    success = self.research_queue_append(drop=drop)
                    if success:
                        total += 1
                    else:
                        logger.info(f'[Исследование — очередь] Не удалось запустить проект; заполнение очереди прекращено, добавлено: {total}')
                        return total
                else:
                    break

            # Запускаем проект вне очереди.
            status = self.get_research_status(self.device.image)
            if 'waiting' not in status:
                logger.info('[Исследование — шестой] Выбор шестого проекта')
                self.research_queue_append(drop=drop, add_queue=False)
            else:
                logger.info('[Исследование — шестой] Шестой проект уже ожидает')

            logger.info(f'[Исследование — очередь] Очередь исследований заполнена, добавлено: {total}')
            return total

    def receive_6th_research(self, skip_first_screenshot=True):
        """
        Результат:
            bool: успешно ли обработан проект.
        """
        logger.hr('Получение шестого проекта', level=2)

        # Ждём завершения анимации.
        timeout = Timer(2, count=6).start()
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if timeout.reached():
                logger.warning('[Исследование — шестой] Тайм-аут ожидания')
                break

            status = self.get_research_status(self.device.image)
            # Карточки проектов ещё не загрузились полностью.
            if 'unknown' in status:
                continue
            # При входе на страницу Research проект waiting появляется на второй
            # позиции, а затем перемещается на третью.
            # После получения награды в очереди проект waiting появляется на
            # четвёртой позиции, а затем перемещается на третью.
            # В обычном состоянии проект waiting должен находиться на третьей позиции.
            if 'waiting' in status:
                if status.index('waiting') == 2:
                    break
                else:
                    continue
            # Проекта вне очереди нет.
            if sum([s == 'detail' for s in status]) == 5:
                break

        # Проверяем, есть ли завершённый проект.
        if self.research_has_finished():
            logger.info(f'[Исследование — шестой] Шестой проект завершён, позиция: {self._research_finished_index}')
            success = self.research_receive()
            if not success:
                return False
        else:
            logger.info('[Исследование — шестой] Завершённых проектов нет')

        # Проверяем состояния waiting и running.
        status = self.get_research_status(self.device.image)
        if 'waiting' in status:
            if self.get_queue_slot() > 0:
                self.research_project_start(status.index('waiting'))
            else:
                logger.info('[Исследование — шестой] Очередь заполнена; добавление ожидающего проекта прекращено')
        if 'running' in status:
            if self.get_queue_slot() > 0:
                self.research_project_start(status.index('running'))
            else:
                logger.info('[Исследование — шестой] Очередь заполнена; добавление выполняющегося проекта прекращено')

        return True

    def handle_pending_t_research(self):
        """
        Обрабатывает отложенный проект Research серии T, требующий поручений.

        Для запуска Research серии T нужно выполнить поручения. Метод проверяет,
        есть ли такой ожидающий проект, и пытается его запустить. После успешного
        запуска количество поручений записывается в конфигурацию, а модуль поручений
        выполняет их.

        Результат:
            bool: True, если Research серии T обработан или отсутствует;
                  False, если Research серии T всё ещё ожидает.
        """
        if self.config.Research_RemainingCommissions <= -1:
            return True
        if self.config.Research_RemainingCommissions > 0:
            return False

        slot = self.get_queue_slot()
        add_queue = slot > 0
        if not self.research_project_start(2, add_queue=add_queue):
            logger.warning('[Исследование — серия T] Не удалось запустить отложенный проект типа T')
            return False

        if add_queue:
            self.config.Research_RemainingCommissions = -1
            return True

        logger.info('[Исследование — серия T] Проект типа T выполняется вне очереди')
        return False

    def run(self):
        """
        Страницы:
            in: любая страница;
            out: page_research с информацией о проектах Research или page_main.
        """
        self.ui_ensure(page_research)

        # Проверяем очередь.
        self.queue_enter()
        self.queue_receive()
        self.end_time = self.get_research_ended()
        self.queue_quit()

        # Обрабатываем отложенный Research серии T.
        if self.handle_pending_t_research():
            # Проверяем проект вне очереди.
            self.receive_6th_research()
            # Заполняем очередь.
            self.research_fill_queue()

        slot = self.get_queue_slot()
        # Планирование.
        if slot == 5:
            # Очередь пуста; нельзя запустить новый Research.
            self.config.task_delay(server_update=True)
            return
        elif self.end_time <= current_time():
            # Получаем оставшееся время нового проекта.
            self.queue_enter()
            self.end_time = self.get_research_ended()
            self.queue_quit()
        if slot == 4:
            # Очередь скоро опустеет; при нехватке ресурсов откладываем Research
            # на 10 минут, чтобы избежать простоя.
            self.end_time = self.end_time + timedelta(minutes=-10)
        self.config.task_delay(target=self.end_time)
