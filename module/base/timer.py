"""Модуль таймеров и инструментов работы со временем.

Предоставляет класс двойного таймера Timer (для подсчёта времени и обращений),
декоратор timer для отладки, а также функции разбора временных строк, такие как future_time.
"""

from time import monotonic as time, sleep
from datetime import timedelta
from functools import wraps

from module.config.time_source import now as current_time


def timer(function):
    """Декоратор замера времени выполнения, используется только для отладки."""

    @wraps(function)
    def function_timer(*args, **kwargs):
        start = time()
        result = function(*args, **kwargs)
        cost = time() - start
        print(f'{function.__name__}: {cost:.10f} s')
        return result

    return function_timer


def future_time(string):
    """Разобрать строку времени и вернуть ближайший будущий соответствующий момент.

    Args:
        string (str): Строка времени, например "14:59".

    Returns:
        datetime.datetime: Ближайший будущий момент с соответствующими часами и минутами.
    """
    hour, minute = [int(x) for x in string.split(':')]
    now = current_time()
    future = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    future = future + timedelta(days=1) if future < now else future
    return future


def past_time(string):
    """Разобрать строку времени и вернуть ближайший прошедший соответствующий момент.

    Args:
        string (str): Строка времени, например "14:59".

    Returns:
        datetime.datetime: Ближайший прошедший момент с соответствующими часами и минутами.
    """
    hour, minute = [int(x) for x in string.split(':')]
    now = current_time()
    past = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    past = past - timedelta(days=1) if past > now else past
    return past


def future_time_range(string):
    """Разобрать строку диапазона времени и вернуть будущее время начала и окончания.

    Args:
        string (str): Строка диапазона времени, например "23:30-06:30".

    Returns:
        tuple[datetime.datetime, datetime.datetime]: (момент начала, момент окончания).
    """
    start, end = [future_time(s) for s in string.split('-')]
    if start > end:
        start = start - timedelta(days=1)
    return start, end


def time_range_active(time_range):
    """Проверить, попадает ли текущее время в указанный интервал.

    Args:
        time_range (tuple[datetime.datetime, datetime.datetime]): (момент начала, момент окончания).

    Returns:
        bool: Возвращает True, если текущее время находится в диапазоне.
    """
    return time_range[0] < current_time() < time_range[1]


class Timer:
    """Двойной таймер, поддерживающий как подсчёт времени, так и подсчёт обращений.

    Подсчёт обращений обеспечивает надёжность на медленных устройствах:
    когда снятие скриншота занимает больше времени, чем лимит таймера,
    условие срабатывания всё ещё может быть определено по числу обращений.
    """

    def __init__(self, limit, count=0):
        """Инициализировать таймер.

        Args:
            limit (int | float): Лимит времени в секундах.
            count (int): Лимит количества обращений, по умолчанию 0.
        """
        self.limit = limit
        self.count = count
        self._start = 0.
        self._access = 0

    @classmethod
    def from_seconds(cls, limit, speed=0.5):
        """Создать таймер на основе заданного числа секунд с автоматическим расчётом числа обращений.

        Args:
            limit (int | float): Лимит времени в секундах.
            speed (int | float): Примерное время создания скриншота в секундах.
                Если время превышает 0.5 секунды, устройство считается медленным.
        """
        count = int(limit / speed)
        return cls(limit, count=count)

    def start(self) -> Timer:
        """Запустить таймер.

        Если таймер не запущен, reached() всегда возвращает True,
        что обеспечивает быструю первую попытку:

        ```python
        interval = Timer(2)
        while 1:
            if interval.reached():
                pass
        ```

        Returns:
            Timer: Сам экземпляр для цепочечных вызовов.
        """
        if self._start <= 0:
            self._start = time()
            self._access = 0

        return self

    def started(self):
        """Проверить, запущен ли таймер.

        Returns:
            bool: Возвращает True, если таймер запущен.
        """
        return self._start > 0

    def current_time(self):
        """Получить время, прошедшее с момента запуска таймера.

        Returns:
            float: Прошедшее количество секунд, либо 0.0, если таймер не запущен.
        """
        if self._start > 0:
            diff = time() - self._start
            if diff < 0:
                diff = 0.
            return diff
        else:
            return 0.

    def current_count(self):
        """Получить текущее количество обращений.

        Returns:
            int: Текущее число обращений.
        """
        return self._access

    def add_count(self):
        """Вручную увеличить счётчик обращений на единицу.

        Returns:
            Timer: Сам экземпляр для цепочечных вызовов.
        """
        self._access += 1
        return self

    def reached(self):
        """Проверить, выполнено ли условие срабатывания таймера.

        Каждый вызов reached() учитывается как одно обращение.
        Для возврата True должны одновременно выполниться лимит обращений и лимит времени.

        Returns:
            bool: True, если условие выполнено; до запуска таймера всегда возвращает True (для первой быстрой попытки).
        """
        # Каждый вызов reached() считается одним обращением
        self._access += 1
        if self._start > 0:
            return self._access > self.count and time() - self._start > self.limit
        else:
            # До запуска возвращаем True для первой быстрой попытки
            return True

    def reset(self):
        """Сбросить таймер, как будто он только что запущен.

        Returns:
            Timer: Сам экземпляр для цепочечных вызовов.
        """
        self._start = time()
        self._access = 0
        return self

    def clear(self):
        """Очистить таймер, как будто он никогда не запускался.

        Returns:
            Timer: Сам экземпляр для цепочечных вызовов.
        """
        self._start = 0.
        self._access = self.count
        return self

    def reached_and_reset(self):
        """Проверить, выполнено ли условие срабатывания, и автоматически сбросить таймер при выполнении.

        Returns:
            bool: True, если условие выполнено и таймер сброшен, иначе False.
        """
        if self.reached():
            self.reset()
            return True
        else:
            return False

    def wait(self):
        """Блокирующее ожидание до достижения таймером лимита времени."""
        diff = self._start + self.limit - time()
        if diff > 0:
            sleep(diff)

    def show(self):
        """Вывести текущее состояние таймера в лог."""
        from module.logger import logger
        logger.info(str(self))

    def __str__(self):
        # Timer(limit=2.351/3, count=4/6)
        return f'Timer(limit={round(self.current_time(), 3)}/{self.limit}, count={self._access}/{self.count})'

    __repr__ = __str__
