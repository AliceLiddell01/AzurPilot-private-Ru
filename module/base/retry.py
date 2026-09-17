"""Модуль декоратора повторных попыток.

Скопирован из библиотеки retry и модифицирован; предоставляет декораторы
повторных попыток с экспоненциальным откатом (backoff), джиттером (jitter)
и настраиваемой обработкой исключений для автоматического перезапуска сбойных операций.
"""

import functools
import random
import time
from functools import partial

from module.logger import logger as logging_logger

"""
Скопировано из библиотеки `retry` с изменениями.
"""

try:
    from decorator import decorator
except ImportError:
    def decorator(caller):
        """Преобразовать caller в декоратор.

        В отличие от модуля decorator, сигнатура функции не сохраняется.

        Args:
            caller: Функция вызова с сигнатурой вида caller(f, *args, **kwargs).
        """

        def decor(f):
            @functools.wraps(f)
            def wrapper(*args, **kwargs):
                return caller(f, *args, **kwargs)

            return wrapper

        return decor


def __retry_internal(f, exceptions=Exception, tries=-1, delay=0, max_delay=None, backoff=1, jitter=0,
                     logger=logging_logger):
    """Выполнить функцию и повторить попытку при сбое.

    Args:
        f: Выполняемая функция.
        exceptions: Перехватываемое исключение или кортеж исключений. По умолчанию Exception.
        tries: Максимальное число попыток. По умолчанию -1 (не ограничено).
        delay: Начальная задержка между повторными попытками в секундах. По умолчанию 0.
        max_delay: Максимальная задержка. По умолчанию None (без ограничений).
        backoff: Коэффициент умножения задержки повтора. По умолчанию 1 (без отката).
            Если число — фиксированное значение, если кортеж (min, max) — случайный диапазон.
        jitter: Дополнительные секунды задержки повтора. По умолчанию 0.
            Если число — фиксированное значение, если кортеж (min, max) — случайный диапазон.
        logger: При сбое вызывает logger.warning(fmt, error, delay).
            По умолчанию retry.logging_logger. Если None, логирование отключено.

    Returns:
        Возвращаемое значение функции f.
    """
    _tries, _delay = tries, delay
    while _tries:
        try:
            return f()
        except exceptions as e:
            _tries -= 1
            if not _tries:
                # В отличие от оригинальной версии, выбрасываем исходное исключение.
                raise e

            if logger is not None:
                # В отличие от оригинальной версии, показываем подробности исключения.
                logger.exception(e)
                logger.warning(f'{type(e).__name__}({e}), повторная попытка через {_delay} с...')

            time.sleep(_delay)
            _delay *= backoff

            if isinstance(jitter, tuple):
                _delay += random.uniform(*jitter)
            else:
                _delay += jitter

            if max_delay is not None:
                _delay = min(_delay, max_delay)


def retry(exceptions=Exception, tries=-1, delay=0, max_delay=None, backoff=1, jitter=0, logger=logging_logger):
    """Вернуть декоратор повторных попыток.

    Args:
        exceptions: Перехватываемое исключение или кортеж исключений. По умолчанию Exception.
        tries: Максимальное число попыток. По умолчанию -1 (не ограничено).
        delay: Начальная задержка между повторными попытками в секундах. По умолчанию 0.
        max_delay: Максимальная задержка. По умолчанию None (без ограничений).
        backoff: Коэффициент умножения задержки повтора. По умолчанию 1 (без отката).
        jitter: Дополнительные секунды задержки повтора. По умолчанию 0.
            Если число — фиксированное значение, если кортеж (min, max) — случайный диапазон.
        logger: При сбое вызывает logger.warning(fmt, error, delay).
            По умолчанию retry.logging_logger. Если None, логирование отключено.

    Returns:
        Декоратор повторных попыток.
    """

    @decorator
    def retry_decorator(f, *fargs, **fkwargs):
        args = fargs if fargs else list()
        kwargs = fkwargs if fkwargs else dict()
        return __retry_internal(partial(f, *args, **kwargs), exceptions, tries, delay, max_delay, backoff, jitter,
                                logger)

    return retry_decorator


def retry_call(f, fargs=None, fkwargs=None, exceptions=Exception, tries=-1, delay=0, max_delay=None, backoff=1,
               jitter=0,
               logger=logging_logger):
    """Вызвать функцию и повторить выполнение при сбое.

    Args:
        f: Выполняемая функция.
        fargs: Позиционные аргументы функции.
        fkwargs: Именованные аргументы функции.
        exceptions: Перехватываемое исключение или кортеж исключений. По умолчанию Exception.
        tries: Максимальное число попыток. По умолчанию -1 (не ограничено).
        delay: Начальная задержка между повторными попытками в секундах. По умолчанию 0.
        max_delay: Максимальная задержка. По умолчанию None (без ограничений).
        backoff: Коэффициент умножения задержки повтора. По умолчанию 1 (без отката).
        jitter: Дополнительные секунды задержки повтора. По умолчанию 0.
            Если число — фиксированное значение, если кортеж (min, max) — случайный диапазон.
        logger: При сбое вызывает logger.warning(fmt, error, delay).
            По умолчанию retry.logging_logger. Если None, логирование отключено.

    Returns:
        Возвращаемое значение функции f.
    """
    args = fargs if fargs else list()
    kwargs = fkwargs if fkwargs else dict()
    return __retry_internal(partial(f, *args, **kwargs), exceptions, tries, delay, max_delay, backoff, jitter, logger)
