"""
Управление очередью Research.

Модуль управляет очередью системы Research, включая:
- добавление запущенных проектов в очередь;
- определение состояния каждого слота (завершён, выполняется, ожидает, пуст);
- получение наград завершённых проектов очереди;
- получение оставшегося времени и расчёт ожидаемого завершения первого проекта.

Очередь Research вмещает не более 5 проектов и работает в порядке FIFO.
После завершения первого проекта ожидающий проект запускается автоматически.

Термины:
    Research Queue: очередь не более чем из 5 проектов;
    Slot: позиция в очереди, нумерация снизу вверх от 0 до 4.
"""
import re
from datetime import timedelta

from module.base.button import ButtonGrid
from module.base.decorator import cached_property, Config
from module.base.utils import get_color
from module.config.time_source import now as current_time
from module.exception import ResearchQueueStateError
from module.logger import logger
from module.ocr.ocr import Duration, Ocr
from module.research.assets import *
from module.research.ui import ResearchUI

OCR_QUEUE_REMAIN = Duration(QUEUE_REMAIN, letter=(255, 255, 255), threshold=128, name='OCR_QUEUE_REMAIN')

_QUEUE_REMAIN_DURATION_RE = re.compile(r'^\s*(\d{1,2}):?(\d{2}):?(\d{2})\s*$')
_QUEUE_REMAIN_OCR_ATTEMPTS = 3
# Запас должен быть больше существующего 10-минутного раннего запуска при одном проекте,
# иначе временная ошибка OCR снова может превратиться в немедленный повтор задачи.
_QUEUE_REMAIN_OCR_RECHECK_DELAY = timedelta(minutes=15)


def _parse_queue_remain_duration(text):
    """Строго разбирает длительность очереди, не угадывая пропущенные OCR-цифры."""
    if not isinstance(text, str):
        return None

    result = _QUEUE_REMAIN_DURATION_RE.search(text)
    if result is None:
        return None

    hours, minutes, seconds = (int(value) for value in result.groups())
    if minutes >= 60 or seconds >= 60:
        return None
    return timedelta(hours=hours, minutes=minutes, seconds=seconds)


class ResearchQueue(ResearchUI):
    """
    Менеджер очереди Research, отвечающий за операции и определение состояния.

    Предоставляет добавление проектов в очередь, определение состояния, получение
    наград и чтение времени. Состояние слотов определяется по цвету значков слева
    от очереди.

    Атрибуты:
        queue_status_grids (ButtonGrid): сетка кнопок значков состояния очереди.
            Из-за различий UI серверов свойство определяется отдельно через
            @Config.when для каждого сервера.
    """
    def research_queue_add(self, skip_first_screenshot=True):
        """
        Результат:
            bool: True, если проект добавлен в очередь; False, если условия проекта
                не выполнены и добавить его нельзя.

        Страницы:
            in: RESEARCH_QUEUE_ADD (is_in_research, DETAIL_NEXT)
            out: is_in_research и стабильный список проектов
        """
        logger.hr('Добавление в очередь исследований')
        # POPUP_CONFIRM только что нажата в research_project_start().
        self.popup_interval_clear()
        self.interval_clear([RESEARCH_QUEUE_ADD])
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            # Завершение.
            if self.is_research_stabled():
                break

            if self.appear(RESEARCH_QUEUE_ADD, offset=(20, 20), interval=5):
                if self._research_queue_add_available():
                    self.device.click(RESEARCH_QUEUE_ADD)
                    continue
                else:
                    logger.info('[Исследование — очередь] Условия проекта не выполнены; отмена')
                    self.research_detail_cancel()
                    return False

            if self.handle_popup_confirm('RESEARCH_QUEUE'):
                self.interval_reset(RESEARCH_QUEUE_ADD)
                continue

        self.ensure_research_center_stable()
        return True

    def _research_queue_add_available(self):
        """
        Результат:
            bool: True, если проект можно добавить в очередь; False, если условия
                проекта не выполнены и добавить его нельзя.
        """
        # RESEARCH_QUEUE_ADD.area — область текста Queue.
        # RESEARCH_QUEUE_ADD.button — вся кликабельная область кнопки.
        # Доступное состояние: (90, 142, 203).
        # Недоступное состояние: (153, 160, 170).
        r, g, b = get_color(self.device.image, RESEARCH_QUEUE_ADD.button)
        if b - min(r, g) > 60:
            return True
        else:
            return False

    def is_in_queue(self, interval=0):
        """Не считает затемнённый фон очереди активной страницей при открытом окне награды."""
        if self.get_items() is not None:
            return False
        return super().is_in_queue(interval=interval)

    @cached_property
    @Config.when(SERVER='en')
    def queue_status_grids(self):
        """
        Значки состояния слева.
        """
        return ButtonGrid(
            origin=(8, 259), delta=(0, 40.5), button_shape=(25, 25), grid_shape=(1, 5), name='QUEUE_STATUS')

    @cached_property
    @Config.when(SERVER='jp')
    def queue_status_grids(self):
        """
        Значки состояния слева.
        """
        return ButtonGrid(
            origin=(18, 259), delta=(0, 40.5), button_shape=(25, 25), grid_shape=(1, 5), name='QUEUE_STATUS')

    @cached_property
    @Config.when(SERVER='tw')
    def queue_status_grids(self):
        """
        Значки состояния слева.
        """
        return ButtonGrid(
            origin=(8, 259), delta=(0, 40.5), button_shape=(25, 25), grid_shape=(1, 5), name='QUEUE_STATUS')

    @cached_property
    @Config.when(SERVER=None)
    def queue_status_grids(self):
        """
        Значки состояния слева.
        """
        return ButtonGrid(
            origin=(18, 259), delta=(0, 40.5), button_shape=(25, 25), grid_shape=(1, 5), name='QUEUE_STATUS')

    def _queue_status_detect(self, button):
        """
        Аргументы:
            button: кнопка значка состояния.

        Результат:
            str:
                'finished': оранжевая галочка в оранжевой рамке;
                'running': чёрная галочка на фоне хода Research, серого и синего;
                'waiting': серое многоточие в серой рамке;
                'empty': чёрное многоточие в чёрной рамке или отсутствие значка.
        """
        center = button.crop((7, 7, 21, 21))
        if self.image_color_count(center, color=(255, 158, 57), threshold=180, count=20):
            return 'finished'
        if self.image_color_count(center, color=(90, 97, 132), threshold=221, count=10):
            return 'waiting'
        if self.image_color_count(center, color=(24, 24, 41), threshold=221, count=10):
            below = button.crop((7, 14, 21, 21))
            if self.image_color_count(below, color=(24, 24, 41), threshold=221, count=10):
                return 'running'
            else:
                return 'empty'
        logger.warning(f'[Исследование — очередь] Неизвестное состояние очереди из {button}; считаем выполняющимся')
        return 'running'

    def get_queue_slot(self):
        """
        Результат:
            int: количество пустых слотов очереди.

        Страницы:
            in: is_in_queue
        """
        status = [self._queue_status_detect(button) for button in self.queue_status_grids.buttons]
        logger.info(f'[Исследование — очередь] Очередь исследований: {status}')
        status = status[::-1]
        for index, s in enumerate(status):
            if s != 'empty':
                logger.attr('Слот очереди исследований', index)
                return index
        index = len(status)
        logger.attr('Слот очереди исследований', index)
        return index

    def _read_queue_remain_duration(self):
        """Читает оставшееся время несколькими свежими кадрами и возвращает только валидное значение."""
        for attempt in range(1, _QUEUE_REMAIN_OCR_ATTEMPTS + 1):
            raw = Ocr.ocr(OCR_QUEUE_REMAIN, self.device.image)
            if isinstance(raw, list):
                raw = raw[0] if raw else ''

            duration = _parse_queue_remain_duration(raw)
            if duration is not None:
                return duration

            logger.warning(
                f'[OCR] OCR_QUEUE_REMAIN: недопустимая длительность {raw!r}; '
                f'попытка {attempt}/{_QUEUE_REMAIN_OCR_ATTEMPTS}'
            )
            if attempt < _QUEUE_REMAIN_OCR_ATTEMPTS:
                self.device.screenshot()

        return None

    def get_research_ended(self):
        """
        Результат:
            datetime: время завершения первого проекта в очереди.

        Страницы:
            in: is_in_queue

        Выбрасывает:
            ResearchQueueStateError:
        """
        now = current_time()

        # Защитный барьер: затемнение от окна награды не должно участвовать
        # в цветовой диагностике состояния первого проекта.
        if self.get_items() is not None:
            end_time = now + _QUEUE_REMAIN_OCR_RECHECK_DELAY
            logger.warning('[Исследование — очередь] Окно награды ещё открыто; '
                           'чтение времени очереди отложено')
            logger.info(f'[Исследование — очередь] Время повторной проверки: {end_time}')
            return end_time

        if self.image_color_count(QUEUE_REMAIN, color=(123, 125, 123), threshold=235, count=100):
            logger.error('[Исследование — очередь] Первый проект в очереди не запущен; '
                         'возможно, это ошибка игры. '
                         'Перезапуск игры должен помочь.')
            raise ResearchQueueStateError(
                '[Исследование — очередь] Первый проект в очереди не запущен; '
                'возможна ошибка состояния игры'
            )
        if not self.image_color_count(QUEUE_REMAIN, color=(255, 255, 255), threshold=221, count=100):
            logger.info('[Исследование — очередь] Очередь исследований пуста')
            return now

        remain = self._read_queue_remain_duration()
        if remain is None:
            end_time = now + _QUEUE_REMAIN_OCR_RECHECK_DELAY
            logger.warning(
                '[Исследование — очередь] OCR оставшегося времени не стабилизировался; '
                f'повторная проверка отложена на {_QUEUE_REMAIN_OCR_RECHECK_DELAY}'
            )
        else:
            end_time = now + remain

        logger.info(f'[Исследование — очередь] Время завершения первого проекта: {end_time}')
        return end_time
