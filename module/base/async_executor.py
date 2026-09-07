"""Модуль асинхронного исполнителя.

Класс AsyncExecutor работает как singleton и поддерживает цикл событий asyncio
в фоновом потоке.
Он передаёт блокирующие операции хранения и отправки в фоновую очередь, чтобы
не блокировать основной поток.
"""

# -*- coding: utf-8 -*-
import asyncio
import inspect
import threading
from typing import Callable, Any

from module.logger import logger

class AsyncExecutor:
    """Асинхронный исполнитель с циклом событий в фоновом потоке."""
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(AsyncExecutor, cls).__new__(cls)
                cls._instance._init()
            return cls._instance

    def _init(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="AsyncExecutorThread")
        self._thread.start()

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        except Exception as e:
            logger.exception(f"Исключение в цикле событий AsyncExecutor: {e}")

    def submit(self, func: Callable, *args, **kwargs) -> asyncio.Future:
        """Поставить синхронную или асинхронную функцию в очередь."""
        if inspect.iscoroutinefunction(func):
            return asyncio.run_coroutine_threadsafe(func(*args, **kwargs), self._loop)
        else:
            # Обычную функцию запускаем в цикле как coroutine.
            # Поэтому записи SQLite выполняются последовательно в его потоке.
            async def wrapper():
                return func(*args, **kwargs)
            return asyncio.run_coroutine_threadsafe(wrapper(), self._loop)

    def flush(self, timeout: float = 5.0):
        """
        等待队列内已有任务尽量执行完毕。
        利用提交一个空任务并等待返回值，实现简单的 flush 效果。
        """
        try:
            future = self.submit(lambda: None)
            future.result(timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("[Асинхронный исполнитель] Истекло время ожидания завершения задач")
        except Exception as e:
            logger.warning(f"[Асинхронный исполнитель] Ошибка при завершении задач: {e}")


# Единственный глобальный экземпляр.
async_executor = AsyncExecutor()

import atexit
atexit.register(async_executor.flush)
