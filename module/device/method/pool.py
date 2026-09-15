"""工作线程池。基于生产者-消费者模型的通用任务池，
用于并发执行截图、控制等设备操作，支持优雅关闭和异常传播。"""

import abc
import ctypes
import subprocess
from collections import deque
from functools import wraps
from itertools import count
from threading import Lock, Thread
from typing import Generic, NoReturn, TypeVar

from module.logger import logger

ValueT = TypeVar("ValueT", covariant=True)
ResultT = TypeVar("ResultT")


def remove_tb_frames(exc, n: int):
    """
    Args:
        exc (BaseException):
        n:

    Returns:
        BaseException:
    """
    tb = exc.__traceback__
    for _ in range(n):
        assert tb is not None
        tb = tb.tb_next
    return exc.with_traceback(tb)


class Outcome(abc.ABC, Generic[ValueT]):
    @abc.abstractmethod
    def unwrap(self) -> ValueT:
        """返回或抛出包含的值或异常。

        以下两行代码是等价的::

           x = fn(*args)
           x = outcome.capture(fn, *args).unwrap()

        """
        pass


class Value(Outcome[ValueT], Generic[ValueT]):
    """表示常规值的 :class:`Outcome` 具体子类。

    """
    __slots__ = ('value',)

    def __init__(self, value: ValueT):
        self.value: ValueT = value

    def __repr__(self) -> str:
        return f'Value({self.value!r})'

    def unwrap(self) -> ValueT:
        return self.value


class Error(Outcome[NoReturn]):
    """表示已抛出异常的 :class:`Outcome` 具体子类。

    """
    __slots__ = ('error',)

    def __init__(self, error: BaseException):
        self.error: BaseException = error

    def __repr__(self) -> str:
        return f'Error({self.error!r})'

    def unwrap(self):
        # Traceback показывает расположенную ниже строку 'raise' вне контекста, поэтому этой переменной
        # даём имя, которое остаётся понятным и вне контекста.
        captured_error = self.error
        try:
            raise captured_error
        finally:
            # Здесь нужно избежать создания цикла ссылок. Python умеет корректно собирать циклические ссылки,
            # поэтому даже созданный цикл не катастрофичен, но циклический сборщик мусора
            # увеличивает задержки программы Python: чем больше создаётся циклов, тем чаще запускается сборщик,
            # поэтому лучше не создавать их изначально. Подробнее см.:
            #
            #    https://github.com/python-trio/trio/issues/1770
            #
            # В частности, удаление этих локальных переменных из frame метода 'unwrap'
            # не позволяет __traceback__ объекта 'captured_error' косвенно ссылаться
            # на сам 'captured_error'.
            del captured_error, self


def capture(sync_fn, *args, **kwargs):
    """
    运行 ``sync_fn(*args, **kwargs)`` 并捕获结果。

    Args:
        sync_fn (Callable[..., ResultT]):

    Returns:
        Value[ResultT] | Error:
    """
    try:
        return Value(sync_fn(*args, **kwargs))
    except BaseException as exc:
        exc = remove_tb_frames(exc, 1)
        return Error(exc)


class JobError(Exception):
    pass


class JobTimeout(Exception):
    pass


class _JobKill(Exception):
    pass


class Job(Generic[ResultT]):
    """
    简单队列，从 queue.Queue() 复制而来。
    更快但只能 put() 一次和 get() 一次。
    """

    # __slots__ = ('worker', 'func_args_kwargs', 'queue', 'mutex', 'finished')

    def __init__(self, worker, func_args_kwargs):
        # Наличие атрибута "worker" означает, что задача выполняется
        # Отсутствие атрибута "worker" означает, что задача завершена или остановлена
        self.worker = worker
        self.func_args_kwargs = func_args_kwargs

        self.queue: "deque[Outcome[ResultT]]" = deque()
        self.put_lock = Lock()
        self.notify_get = Lock()
        self.notify_get.acquire()

    def __repr__(self):
        return f'Job({self.func_args_kwargs})'

    def get(self) -> ResultT:
        """
        获取任务结果或任务错误。
        """
        self.notify_get.acquire()

        # Возвращаем результат задачи или выбрасываем её ошибку
        item = self.queue.popleft()
        return item.unwrap()

    def get_or_kill(self, timeout) -> ResultT:
        """
        尝试在给定秒数内获取结果。
        成功则返回任务结果或任务错误，失败则终止任务并抛出 JobTimeout。

        注意当线程池已满时，JobTimeout 可能不会立即抛出。
        """
        if self.notify_get.acquire(timeout=timeout):
            # Возвращаем результат задачи или выбрасываем её ошибку
            item = self.queue.popleft()
            return item.unwrap()
        else:
            self._kill()
            raise JobTimeout

    def _kill(self):
        with self.put_lock:
            try:
                worker = self.worker
            except AttributeError:
                # Попытка остановить уже завершённую задачу ничего не делает
                return
            worker.kill()
            del self.worker


name_counter = count()


class WorkerThread:
    def __init__(self, thread_pool):
        """
        Args:
            thread_pool (WorkerPool):
        """
        self.job: "Job | None" = None
        self.thread_pool = thread_pool
        # Этот Lock используется нестандартным образом.
        #
        # "Разблокирован" означает, что нам назначена ожидающая задача;
        # "Заблокирован" означает, что ожидающих задач нет.
        #
        # Изначально задач нет, поэтому начинаем в заблокированном состоянии.
        self.worker_lock = Lock()
        self.worker_lock.acquire()
        self.default_name = f"Alasio thread {next(name_counter)}"

        self.thread = Thread(target=self._work, name=self.default_name, daemon=True)
        self.thread.start()

    def __repr__(self):
        return f'{self.__class__.__name__}({self.default_name})'

    def _handle_job(self) -> None:
        # Переносим в локальную переменную: если назначат новую задачу, `self.job` уже будет содержать другое значение
        job = self.job
        del self.job
        func, args, kwargs = job.func_args_kwargs

        result = capture(func, *args, **kwargs)

        # Уведомляем пул потоков, что снова свободны и можем принять новую задачу.
        # Делаем это до вызова 'deliver', чтобы, если 'deliver' породит новую задачу,
        # её можно было назначить нам вместо создания нового потока.
        self.thread_pool.idle_workers[self] = None
        self.thread_pool.release_full_lock()

        # Передаём результат
        if isinstance(result, Error) and isinstance(result.error, _JobKill):
            # Задача была остановлена
            pass
        else:
            # Задача завершена: помещаем результат и уведомляем ожидающего
            with job.put_lock:
                job.queue.append(result)
                del job.worker
                job.notify_get.release()

    def _work(self) -> None:
        while True:
            if self.worker_lock.acquire(timeout=WorkerPool.IDLE_TIMEOUT):
                # Получили задачу
                self._handle_job()
            else:
                # Ожидание lock истекло, можно завершаться. Но есть состояние гонки:
                # задача могла быть назначена прямо перед выходом, поэтому нужно проверить.
                try:
                    del self.thread_pool.idle_workers[self]
                except KeyError:
                    # Другой поток уже удалил нас из очереди свободных,
                    # значит, нам назначают задачу — продолжаем цикл ожидания.
                    self.thread_pool.release_full_lock()
                    continue
                else:
                    # Успешно удалили себя из очереди свободных: новых задач уже не будет, можно безопасно завершаться.
                    del self.thread_pool.all_workers[self]
                    self.thread_pool.release_full_lock()
                    return

    def kill(self):
        """
        终止线程确实不安全，但当单个任务函数阻塞时别无选择。
        此方法应受 `job.put_lock` 保护，以防止与 `_handle_job()` 的竞态条件。

        Returns:
            bool: 是否成功终止线程
        """
        # Отправляем потоку SystemExit
        thread_id = ctypes.c_long(self.thread.ident)
        res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
            thread_id, ctypes.py_object(_JobKill))
        if res <= 1:
            self.thread_pool.all_workers.pop(self, None)
            self.thread_pool.release_full_lock()
            return True
        else:
            try:
                job = self.job
            except AttributeError:
                job = None
            logger.error(f'[Устройство] Не удалось завершить поток {self.thread.ident} из задачи {job}')
            # Не удалось отправить SystemExit — сбрасываем его
            ctypes.pythonapi.PyThreadState_SetAsyncExc(thread_id, 0)
            return False


class WorkerPool:
    """
    模仿 trio.to_thread.start_thread_soon() 的线程池。
    参考: https://github.com/python-trio/trio/issues/6
    """

    # Свободный поток завершается через 10 секунд.
    IDLE_TIMEOUT = 10

    def __init__(self, pool_size: int = 8):
        # В пуле не более 8 потоков.
        # Alasio используется для редкого локального доступа, поэтому пул по умолчанию небольшой
        self.pool_size = pool_size

        self.idle_workers: "dict[WorkerThread, None]" = {}
        self.all_workers: "dict[WorkerThread, None]" = {}

        self.notify_worker = Lock()
        self.notify_worker.acquire()
        self.notify_pool = Lock()
        self.notify_pool.acquire()

    def release_full_lock(self):
        """
        当工作线程完成任务、退出或被终止时调用此方法。

        当线程池已满时，
        线程池通知所有工作线程：任何完成任务的线程请通知我。
        `self.notify_worker.release()`
        然后线程池阻塞自己。
        `self.notify_pool.acquire()`
        最快的工作线程（也是唯一一个）接收到消息。
        `if self.notify_worker.acquire(blocking=False):`
        工作线程通知线程池，新槽位已就绪，可以继续。
        `self.notify_pool.release()`
        """
        if self.notify_worker.acquire(blocking=False):
            self.notify_pool.release()

    def _get_thread_worker(self) -> WorkerThread:
        try:
            worker, _ = self.idle_workers.popitem()
            return worker
        except KeyError:
            pass

        # При достижении максимального числа потоков ждём
        if len(self.all_workers) >= self.pool_size:
            # См. release_full_lock()
            self.notify_worker.release()
            self.notify_pool.acquire()
            # Один из рабочих потоков только что освободился
            try:
                worker, _ = self.idle_workers.popitem()
                return worker
            except KeyError:
                pass
            # Один из рабочих потоков только что завершился
            # if len(self.all_workers) < WorkerPool.MAX_WORKER:
            #     break

        # Создаём новый рабочий поток
        worker = WorkerThread(self)
        # logger.info(f'New worker thread: {worker.default_name}')
        self.all_workers[worker] = None
        return worker

    def start_thread_soon(self, func, *args, **kwargs):
        """
        在线程上运行函数，结果可从 `job` 对象获取。

        Args:
            func (Callable[..., ResultT]):
            *args:
            **kwargs:

        Returns:
            Job[ResultT]:

        Examples:
            job = WORKER_POOL.start_thread_soon(func, *args)
            result = job.get()
        """
        worker = self._get_thread_worker()
        job = Job(worker=worker, func_args_kwargs=(func, args, kwargs))

        worker.job = job
        worker.worker_lock.release()
        return job

    def run_on_thread(self, func):
        """
        装饰器，使函数在线程上运行，结果可从 `job` 对象获取。

        Args:
            func (Callable[..., ResultT]):

        Returns:
            Job[ResultT]:

        Examples:
            @run_on_thread
            def function(...):
                pass
            job = function(...)
            result = job.get()
        """
        @wraps(func)
        def thread_wrapper(*args, **kwargs) -> "Job[ResultT]":
            return self.start_thread_soon(func, *args, **kwargs)

        return thread_wrapper

    @staticmethod
    def _subprocess_execute(cmd, timeout=10):
        """
        在子进程中运行命令的辅助函数。

        Args:
            cmd (list[str]):
            timeout:

        Returns:
            bytes:
        """
        logger.info(f'[Устройство — пул процессов] Выполнение команды: {cmd}')

        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, shell=False)

        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            logger.warning(f'[Устройство — пул процессов] Истёк тайм-аут команды {cmd}, stdout={stdout}, stderr={stderr}')
        return stdout

    def start_cmd_soon(self, cmd, timeout=10):
        """
        在子进程中运行命令并在另一个线程上通信，结果可从 `job` 对象获取。

        Args:
            cmd (list[str]):
            timeout:

        Returns:
            Job[bytes]:
        """
        worker = self._get_thread_worker()
        job = Job(worker=worker, func_args_kwargs=(
            self._subprocess_execute, (cmd,), {'timeout': timeout}
        ))

        worker.job = job
        worker.worker_lock.release()
        return job

    def wait_jobs(self) -> "WaitJobsWrapper":
        """
        自动等待所有任务完成。

        Examples:
            with WORKER_POOL.wait_jobs() as pool:
                pool.start_thread_soon(...)
        """
        return WaitJobsWrapper(self)

    def gather_jobs(self) -> "GatherJobsWrapper":
        """
        自动等待所有任务完成并收集结果。

        Examples:
            pool = WORKER_POOL.gather_jobs()
            with pool:
                pool.start_thread_soon(...)
            # 获取结果
            print(pool.results)
        """
        return GatherJobsWrapper(self)

    def thread_map(self, func, iterables):
        """
        ThreadPoolExecutor.map(func, iterables) 的替代方案。

        Args:
            func (Callable[..., ResultT]):
            iterables:

        Returns:
            list[ResultT]:
        """
        jobs = [self.start_thread_soon(func, arg) for arg in iterables]
        results = [job.get() for job in jobs]
        return results

    def thread_starmap(self, func, iterables):
        """
        multiprocessing.pool.Pool().starmap(func, iterables) 的线程版本替代方案。

        Args:
            func (Callable[..., ResultT]):
            iterables:

        Returns:
            list[ResultT]:
        """
        jobs = [self.start_thread_soon(func, *arg) for arg in iterables]
        results = [job.get() for job in jobs]
        return results

    def thread_funcmap(self, func_iterables):
        """
        在线程上运行一组函数。

        Args:
            func_iterables (Iterable[Callable[..., ResultT]]):

        Returns:
            list[ResultT]:
        """
        jobs = [self.start_thread_soon(func) for func in func_iterables]
        results = [job.get() for job in jobs]
        return results


class WaitJobsWrapper:
    """
    等待所有任务完成的包装类。
    """

    def __init__(self, pool: "WorkerPool"):
        self.pool: "WorkerPool" = pool
        self.jobs: "list[Job[ResultT]]" = []

    def get(self):
        for job in self.jobs:
            job.get()
        self.jobs.clear()

    def __enter__(self):
        self.jobs.clear()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.get()

    def start_thread_soon(self, func, *args, **kwargs):
        """
        在线程上运行函数，结果可从 `job` 对象获取。

        Args:
            func (Callable[..., ResultT]):
            *args:
            **kwargs:

        Returns:
            Job[ResultT]:
        """
        job = self.pool.start_thread_soon(func, *args, **kwargs)
        self.jobs.append(job)
        return job


class GatherJobsWrapper(WaitJobsWrapper):
    """
    收集所有任务结果的包装类。
    """

    def __init__(self, pool: "WorkerPool"):
        super().__init__(pool)
        self.results: "list[ResultT]" = []

    def get(self):
        for job in self.jobs:
            result = job.get()
            self.results.append(result)
        self.jobs.clear()

    def __enter__(self):
        self.jobs.clear()
        self.results.clear()
        return self


WORKER_POOL = WorkerPool()
