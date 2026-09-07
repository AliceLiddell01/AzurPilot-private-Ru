"""
Менеджер процессов экземпляров.

Управляет жизненным циклом процессов экземпляров Alas, включая пул процессов,
отслеживание состояний (работает/остановлен/ошибка) и безопасное межпроцессное взаимодействие.
"""

import argparse

# Этот файл управляет жизненным циклом процессов экземпляров Alas и их дочерних процессов.
# Он поддерживает пул процессов для нескольких аккаунтов, отслеживает состояния
# (работает/остановлен/ошибка) и безопасно обрабатывает межпроцессное взаимодействие.
from collections.abc import Sequence
import os
import queue
import subprocess
import threading
import time
from multiprocessing import Event, Process
from pathlib import Path
from typing import Dict, List, Union

import inflection
from rich.console import Console, ConsoleRenderable
from rich.text import Text

# Этот файл выполняется не в том же процессе или дочернем процессе, что и app.py,
# поэтому следующие действия требуется повторить.
# Подменяем модуль до импорта pywebio, чтобы не загружать лишний PIL.
from module.webui.fake_pil_module import *

import_fake_pil_module()

from module.logger import logger, set_file_logger, set_func_logger
from module.config.utils import DEFAULT_CONFIG_NAME
from module.submodule.submodule import load_mod
from module.submodule.utils import (
    get_available_func,
    get_available_mod,
    get_available_mod_func,
    get_config_mod,
    get_func_mod,
    list_mod_instance,
)
from module.webui.setting import State
from module.webui.worker_registry import (
    get_workers,
    is_current_owner,
    process_matches,
    register_worker,
    unregister_worker,
)

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_RUNTIME_STATE_HEARTBEAT_SECONDS = 15.0
_RUNTIME_STATE_HEARTBEAT_JOIN_SECONDS = 2.0


class ProcessManager:
    _processes: Dict[str, "ProcessManager"] = {}
    _managers_lock = threading.RLock()
    _lifecycle_locks: Dict[str, threading.RLock] = {}
    _lifecycle_locks_lock = threading.Lock()

    def __init__(self, config_name: str = DEFAULT_CONFIG_NAME) -> None:
        self.config_name = config_name
        self._renderable_queue: queue.Queue[ConsoleRenderable] = State.manager.Queue()
        self.renderables: List[ConsoleRenderable] = []
        self.renderables_max_length = 400
        self.renderables_reduce_length = 80
        self._process: Process | None = None
        self._stop_event: object | None = None
        self._runtime_operation_id: str | None = None
        self._runtime_session_id: str | None = None
        self._registry_cleanup_confirmed = False
        self.thd_log_queue_handler: threading.Thread | None = None
        self._state_override: int | None = None
        self._state_override_deadline: float | None = None

    @classmethod
    def _get_lifecycle_lock(cls, config_name: str) -> threading.RLock:
        """Вернуть общую блокировку жизненного цикла экземпляра конфигурации."""
        with cls._lifecycle_locks_lock:
            try:
                return cls._lifecycle_locks[config_name]
            except KeyError:
                lock = threading.RLock()
                cls._lifecycle_locks[config_name] = lock
                return lock

    def set_state_override(self, state: int, duration: float = 10) -> None:
        """
        Принудительно установить временное состояние UI для проверки значка.

        Args:
            state: состояние (1=работает, 2=остановлен, 3=ошибка)
            duration: длительность переопределения в секундах; 0 или None означает ручное снятие
        """
        if state not in (1, 2, 3):
            raise ValueError(f"Недопустимое переопределение состояния: {state}")
        self._state_override = state
        if duration and duration > 0:
            self._state_override_deadline = time.time() + duration
        else:
            self._state_override_deadline = None

    def clear_state_override(self) -> None:
        self._state_override = None
        self._state_override_deadline = None

    def _get_state_override(self) -> int | None:
        if self._state_override is None:
            return None
        if (
            self._state_override_deadline is not None
            and time.time() >= self._state_override_deadline
        ):
            self.clear_state_override()
            return None
        return self._state_override

    def start(
        self,
        func: str | None,
        ev: object | None = None,
        *,
        operation_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        # Транзакция перезапуска WebUI удерживает restart_lock, а очистка — cleanup_lock.
        # Поток запроса не должен надолго блокироваться внутри транзакции.
        if not State.restart_lock.acquire(blocking=False):
            logger.info(f"[{self.config_name}] WebUI перезапускается; запуск рабочего процесса отклонён")
            return
        try:
            if not State.cleanup_lock.acquire(blocking=False):
                logger.info(f"[{self.config_name}] WebUI очищается; запуск рабочего процесса отклонён")
                return
            try:
                with self._get_lifecycle_lock(self.config_name):
                    if State._restart_requested or State._clearup:
                        logger.warning(
                            f"[{self.config_name}] WebUI перезапускается или уже очищена; запуск рабочего процесса отклонён"
                        )
                        return
                    if self.alive:
                        return
                    self._reconcile_runtime_state_before_start()
                    if self.alive:
                        return
                    # При неподтверждённой записи alive консервативно возвращает False;
                    # здесь повторно проверяем registry, чтобы не запустить дублирующий worker.
                    _pid, _, _verified = self._registered_worker()
                    if not _verified and _pid is not None:
                        logger.warning(
                            f"[{self.config_name}] Запись рабочего процесса не согласована; запуск отклонён во избежание дублирования"
                        )
                        return
                    if func is None:
                        func = get_config_mod(self.config_name)
                    if ev is None:
                        ev = Event()
                    self._stop_event = ev
                    self._runtime_operation_id = operation_id
                    self._runtime_session_id = session_id
                    args = (
                        self.config_name,
                        func,
                        self._renderable_queue,
                        ev,
                        str(_REPOSITORY_ROOT),
                        operation_id,
                        session_id,
                    )
                    process = Process(
                        target=ProcessManager.run_process,
                        args=args,
                    )
                    self._process = process
                    try:
                        process.start()
                        self._register_process(process.pid)
                    except Exception:
                        self._terminate_unregistered_process(process)
                        self._process = None
                        self._stop_event = None
                        self._runtime_operation_id = None
                        self._runtime_session_id = None
                        raise
                    self.start_log_queue_handler()
            finally:
                State.cleanup_lock.release()
        finally:
            State.restart_lock.release()

    def start_log_queue_handler(self) -> None:
        log_queue_handler = self.thd_log_queue_handler
        if log_queue_handler is not None and log_queue_handler.is_alive():
            return
        self.thd_log_queue_handler = threading.Thread(
            target=self._thread_log_queue_handler
        )
        self.thd_log_queue_handler.start()

    def stop(self) -> bool:
        """Остановить дерево worker и вернуть признак подтверждённого завершения."""
        with self._get_lifecycle_lock(self.config_name):
            self._registry_cleanup_confirmed = False
            process = self._process
            local_process_alive = self._is_process_alive(process)

            if process is not None:
                pid, record, pid_verified = self._registered_worker(process.pid)
            else:
                pid, record, pid_verified = self._registered_worker()

            # _registered_worker мог вернуть zombie-дескриптор через join(0),
            # либо worker мог завершиться за это время. Обновляем локальный признак жизни,
            # чтобы устаревший local_process_alive не дал ложную ошибку stop.
            if local_process_alive and not self._is_process_alive(self._process):
                local_process_alive = False

            stopped = pid is None and not local_process_alive
            if pid is not None and not pid_verified:
                # _registered_worker мог вернуть zombie-дескриптор через join(0);
                # очищенный дескриптор подтверждает завершение worker.
                if self._is_process_alive(self._process):
                    logger.error(
                        f"[{self.config_name}] Не удалось подтвердить рабочий процесс PID {pid}; завершение неизвестного процесса отклонено"
                    )
                    stopped = False
                else:
                    logger.info(
                        f"[{self.config_name}] Локальный дескриптор рабочего процесса PID {pid} освобождён; процесс завершён"
                    )
                    stopped = True
            elif pid is not None:
                if local_process_alive and process is not None:
                    # Сначала используем terminate/kill локального Process:
                    # это надёжнее, чем taskkill.
                    stopped = ProcessManager._stop_local_process(process)
                    if not stopped:
                        # При ошибке локального дескриптора переходим к taskkill для дерева процессов.
                        stopped = self._kill_registered_process_tree(pid, record)
                        if stopped:
                            process.join(timeout=3)
                            stopped = not self._is_process_alive(process)
                else:
                    stopped = self._kill_registered_process_tree(pid, record)
                    if stopped and process is not None:
                        process.join(timeout=3)
                        stopped = not self._is_process_alive(process)
            if stopped:
                self._process = None
                if self._registry_cleanup_confirmed and record is None:
                    self._stop_event = None
                    self._runtime_operation_id = None
                    self._runtime_session_id = None
                elif record is None and pid is not None and not pid_verified:
                    # Реестр уже содержит другой worker или не подтверждает
                    # прежнюю identity; повторная проверка не должна удалить
                    # чужую запись или сообщить об успешной остановке.
                    stopped = self._unregister_process(expected_worker=None)
                else:
                    stopped = self._unregister_process(expected_worker=record)
                self._registry_cleanup_confirmed = False
                if stopped and pid is not None:
                    self.renderables.append(
                        Text(f"[{self.config_name}] завершён. Причина: ручная остановка\n")
                    )
            if not stopped:
                logger.error(f"[{self.config_name}] Не удалось остановить рабочий процесс PID {pid}")
            log_queue_handler = self.thd_log_queue_handler
            if log_queue_handler is not None:
                log_queue_handler.join(timeout=1)
                if log_queue_handler.is_alive():
                    logger.warning(
                        "[WebUI-процессы] Поток обработки очереди журналов не остановился за 1 секунду"
                    )
        if stopped:
            logger.info(f"[{self.config_name}] Рабочий процесс завершён")
        else:
            logger.warning(f"[{self.config_name}] Рабочий процесс остановлен не полностью")
        return stopped

    def request_cooperative_stop(
        self,
        *,
        operation_id: str | None = None,
        session_id: str | None = None,
    ) -> bool:
        """Попросить worker завершить текущий task без terminate/kill."""

        with self._get_lifecycle_lock(self.config_name):
            if not self.alive:
                return True
            event = self._stop_event
            if event is None or not callable(getattr(event, "set", None)):
                logger.error(
                    f"[{self.config_name}] У worker нет локального cooperative stop event"
                )
                return False
            try:
                from module.application.runtime_state import RuntimeStateStore

                RuntimeStateStore(_REPOSITORY_ROOT).request_quiesce(
                    self.config_name,
                    operation_id=operation_id or self._runtime_operation_id or "runtime",
                    session_id=session_id or self._runtime_session_id,
                )
            except Exception as exc:  # noqa: BLE001 - граница owner работает fail-closed.
                logger.error(
                    f"[{self.config_name}] Не удалось записать cooperative stop state: {exc}"
                )
                return False
            try:
                event.set()
            except Exception as exc:  # noqa: BLE001 - сигнал остановки работает в fail-closed режиме.
                logger.error(
                    f"[{self.config_name}] Не удалось передать cooperative stop request: {exc}"
                )
                return False
            return True

    def wait_for_exit(self, timeout: float) -> bool:
        """Дождаться естественного выхода worker без принудительной эскалации."""

        if type(timeout) not in (int, float) or timeout < 0:
            raise ValueError("timeout должен быть неотрицательным числом")
        deadline = time.monotonic() + float(timeout)
        while True:
            if not self.alive:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.1, remaining))

    @staticmethod
    def _is_process_alive(process: Process | None) -> bool:
        """Прочитать локальное состояние процесса, собрать zombie-дескриптор и считать недействительный дескриптор завершённым.

        Завершённый, но ещё не переданный в join дескриптор
        multiprocessing.Process сообщает is_alive() == True (состояние zombie).
        Метод вызывает join(timeout=0), чтобы сборка дескриптора не искажала
        проверки жизни во время stop. join(timeout=0) не блокирует работающий процесс.
        """
        try:
            if process is None:
                return False
            if not process.is_alive():
                return False
            # Собрать завершённый, но ещё не переданный в join дескриптор.
            process.join(timeout=0)
            return process.is_alive()
        except (OSError, ValueError, AssertionError):
            return False

    @staticmethod
    def _stop_local_process(process: Process) -> bool:
        """Пошагово завершить worker локальным Process, предпочитая его taskkill.

        Сначала вызвать terminate() и ждать 5 секунд, затем при тайм-ауте вызвать kill()
        и ждать 3 секунды. taskkill может молча завершиться из-за прав или состояния
        процесса; локальный дескриптор надёжнее. Метод завершает только корневой процесс,
        а вызывающая сторона при ошибке должна перейти к _kill_process_tree.
        """
        try:
            process.terminate()
        except (OSError, ValueError, AssertionError):
            pass
        process.join(timeout=5)
        if process.is_alive():
            try:
                process.kill()
            except (OSError, ValueError, AssertionError):
                pass
            process.join(timeout=3)
        return not process.is_alive()

    @classmethod
    def _terminate_unregistered_process(cls, process: Process) -> None:
        """Откатить незарегистрированный worker после ошибки запуска через локальный Process."""
        if not cls._is_process_alive(process):
            try:
                process.join(timeout=0)
            except (OSError, ValueError, AssertionError):
                pass
            return

        try:
            # Дескриптор Process привязан к созданному дочернему процессу
            # и не позволяет ошибочно завершить процесс с повторно использованным PID.
            process.terminate()
            process.join(timeout=3)
            if cls._is_process_alive(process):
                process.kill()
                process.join(timeout=3)
        except (OSError, ValueError, AssertionError):
            pass

    def _kill_registered_process_tree(self, pid: int, record: dict | None) -> bool:
        """Повторно проверить identity перед taskkill и уменьшить окно повторного использования PID."""
        if record is None:
            logger.error(f"[{self.config_name}] Для рабочего процесса PID {pid} нет постоянной записи идентичности")
            return False
        try:
            matches = process_matches(record)
        except RuntimeError as exc:
            logger.error(f"[{self.config_name}] Не удалось повторно проверить рабочий процесс PID {pid}: {exc}")
            return False

        if matches is True:
            return self._kill_process_tree(pid)
        if matches is None:
            logger.info(f"[{self.config_name}] Рабочий процесс PID {pid} завершился до принудительной остановки")
            return True

        logger.error(
            f"[{self.config_name}] PID {pid} уже принадлежит другому процессу; завершение неизвестного процесса отклонено"
        )
        return False

    @staticmethod
    def _kill_process_tree(pid: int) -> bool:
        """Завершить worker и производные процессы, чтобы задача не осталась после закрытия WebUI."""
        if os.name == "nt":
            try:
                result = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    timeout=3,
                )
                if result.returncode == 0:
                    return ProcessManager._wait_pid_exit(pid, timeout=3)
                if not ProcessManager._pid_exists(pid):
                    return True
                logger.warning(f"[WebUI-процессы] Не удалось остановить рабочий процесс PID {pid}: taskkill вернул код {result.returncode}")
                return False
            except (OSError, subprocess.TimeoutExpired) as exc:
                logger.warning(f"[WebUI-процессы] Не удалось остановить рабочий процесс PID {pid}: {exc}")
                return False
        else:
            try:
                import psutil

                parent = psutil.Process(pid)
                for child in reversed(parent.children(recursive=True)):
                    try:
                        child.kill()
                    except psutil.NoSuchProcess:
                        pass
            except (ImportError, psutil.Error if "psutil" in locals() else OSError):
                pass
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            return True
        return ProcessManager._wait_pid_exit(pid, timeout=3)

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @staticmethod
    def _wait_pid_exit(pid: int, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not ProcessManager._pid_exists(pid):
                return True
            time.sleep(0.1)
        return not ProcessManager._pid_exists(pid)

    def _registered_worker(
        self, expected_pid: int | None = None
    ) -> tuple[int | None, dict | None, bool]:
        """Вернуть подтверждённую identity worker; вызывающая сторона должна держать lifecycle lock."""
        registry = State.process_registry
        cached_pid = None
        if registry is not None:
            try:
                cached_pid = registry.get(self.config_name)
                cached_pid = int(cached_pid) if cached_pid is not None else None
            except (TypeError, ValueError):
                logger.error(f"[{self.config_name}] Недопустимая запись PID рабочего процесса")
                return expected_pid, None, False
            except Exception as exc:
                logger.error(f"[{self.config_name}] Не удалось прочитать запись PID рабочего процесса: {exc}")
                return expected_pid, None, False

        try:
            expected_pid = int(expected_pid) if expected_pid is not None else None
        except (TypeError, ValueError):
            logger.error(f"[{self.config_name}] Недопустимый локальный PID рабочего процесса")
            return None, None, False

        if expected_pid is not None and cached_pid not in (None, expected_pid):
            logger.error(
                f"[{self.config_name}] Локальный PID рабочего процесса {expected_pid} не совпадает с общей записью {cached_pid}"
            )
            return expected_pid, None, False

        pid = expected_pid if expected_pid is not None else cached_pid
        if pid is None:
            return None, None, True

        try:
            if not is_current_owner(os.getpid()):
                logger.error(
                    f"[{self.config_name}] Текущая WebUI не владеет записью рабочего процесса; операция с PID {pid} отклонена"
                )
                return pid, None, False
            record = get_workers(os.getpid()).get(self.config_name)
            try:
                record_pid = int(record["pid"])
            except (KeyError, TypeError, ValueError):
                record_pid = None
            if not isinstance(record, dict) or record_pid != pid:
                logger.error(
                    f"[{self.config_name}] Для рабочего процесса PID {pid} нет соответствующей постоянной записи"
                )
                return pid, None, False
            matches = process_matches(record)
        except RuntimeError as exc:
            logger.error(f"[{self.config_name}] Не удалось проверить рабочий процесс PID {pid}: {exc}")
            return pid, None, False

        if matches is True:
            return pid, record, True

        if matches is False:
            logger.error(
                f"[{self.config_name}] PID {pid} уже принадлежит другому процессу; устаревшая запись удалена без завершения процесса"
            )
        else:
            logger.info(f"[{self.config_name}] Рабочий процесс PID {pid} завершён; устаревшая запись удалена")

        unregistered = self._unregister_process(expected_worker=record)
        if unregistered:
            self._registry_cleanup_confirmed = True
        if expected_pid is not None:
            # process_matches подтвердил смерть процесса (None) или повторное использование PID
            # (False), но локальный дескриптор может оставаться zombie до join.
            # Собираем его, чтобы не считать завершённый процесс живым.
            try:
                process = self._process
                if process is not None and process.pid == expected_pid:
                    process.join(timeout=0)
            except (OSError, ValueError, AssertionError):
                pass
            # Если после join дескриптор больше не сообщает о жизни, zombie собран.
            if not self._is_process_alive(self._process):
                self._process = None
                if unregistered:
                    return None, None, True
            return expected_pid, None, False
        if unregistered:
            return None, None, True
        return pid, None, False

    def _registered_pid(self) -> tuple[int | None, bool]:
        """Вернуть зарегистрированный PID и признак подтверждённой identity."""
        pid, _, verified = self._registered_worker()
        return pid, verified

    def _reconcile_runtime_state_before_start(self) -> None:
        """Перед новым worker списать только доказанно мёртвый runtime state."""
        from module.application.runtime_state import RuntimeStateStore

        workers = get_workers(os.getpid())

        def check_worker(worker_pid: int, worker_created_at: float) -> bool | None:
            return process_matches(
                {"pid": worker_pid, "created_at": worker_created_at}
            )

        reconciled = RuntimeStateStore(_REPOSITORY_ROOT).reconcile_stale_workers(
            workers,
            worker_identity_checker=check_worker,
        )
        if State.process_registry is not None:
            worker = workers.get(self.config_name)
            if isinstance(worker, dict):
                State.process_registry[self.config_name] = int(worker["pid"])
            else:
                State.process_registry.pop(self.config_name, None)
        if reconciled:
            logger.warning(
                f"[{self.config_name}] Перед запуском восстановлены остановленные runtime-профили: "
                f"{', '.join(reconciled)}"
            )

    def _register_process(self, pid: int | None) -> None:
        if pid is None:
            return
        registered_record = register_worker(os.getpid(), self.config_name, pid)
        record: dict | None = None
        try:
            from module.application.runtime_state import RuntimePhase, RuntimeStateStore

            state_store = RuntimeStateStore(_REPOSITORY_ROOT)
            record = get_workers(os.getpid()).get(self.config_name)
            if not isinstance(record, dict):
                raise RuntimeError("После регистрации отсутствует запись worker")
            worker_pid = int(record["pid"])
            worker_created_at = float(record["created_at"])
            phase = RuntimePhase.USER_PROFILE_IDLE
            current = state_store.read(self.config_name)
            if (
                current is not None
                and current.phase is RuntimePhase.RESOURCE_ACQUIRING
                and current.operation_id == self._runtime_operation_id
                and current.session_id == self._runtime_session_id
            ):
                phase = RuntimePhase.RESOURCE_ACQUIRING
            state_store.mark_worker_started(
                self.config_name,
                worker_pid=worker_pid,
                worker_created_at=worker_created_at,
                operation_id=self._runtime_operation_id,
                session_id=self._runtime_session_id,
                phase=phase,
            )
            if State.process_registry is not None:
                State.process_registry[self.config_name] = pid
        except Exception as exc:  # noqa: BLE001 - при ошибке worker не должен остаться без учёта.
            logger.warning(
                f"[{self.config_name}] Не удалось обновить process-shared runtime state: {exc}"
            )
            try:
                rollback_record = (
                    record
                    if isinstance(record, dict)
                    else registered_record
                    if isinstance(registered_record, dict)
                    else None
                )
                self._unregister_process(expected_worker=rollback_record)
            except Exception as rollback_exc:  # noqa: BLE001 - исходная ошибка остаётся причиной отказа.
                logger.error(
                    f"[{self.config_name}] Не удалось откатить регистрацию worker после ошибки runtime state: {rollback_exc}"
                )
            raise

    def _unregister_process(self, *, expected_worker: dict | None = None) -> bool:
        if expected_worker is None:
            # Отсутствие ожидаемой identity означает, что этот manager больше
            # не имеет права очищать запись, которая могла уже принадлежать
            # новому worker того же профиля.
            if not is_current_owner(os.getpid()):
                return False
            try:
                if get_workers(os.getpid()).get(self.config_name) is not None:
                    logger.error(
                        f"[{self.config_name}] Запись worker существует без подтверждённой identity; очистка отклонена"
                    )
                    return False
            except Exception as exc:  # noqa: BLE001 - отсутствие подтверждённого registry блокирует очистку.
                logger.error(
                    f"[{self.config_name}] Не удалось подтвердить отсутствие записи worker: {exc}"
                )
                return False
            self._stop_event = None
            self._runtime_operation_id = None
            self._runtime_session_id = None
            return True
        try:
            if not unregister_worker(
                os.getpid(),
                self.config_name,
                expected_worker=expected_worker,
            ):
                logger.error(
                    f"[{self.config_name}] Текущая WebUI не владеет записью рабочего процесса; очистка отклонена"
                )
                return False
        except Exception as exc:
            logger.exception_context(
                title='Не удалось очистить запись рабочего процесса',
                exc=exc,
                impact='Родительский процесс повторно проверит этот PID перед следующим перезапуском.',
                action='Проверьте права записи в каталог config.',
                level=40,
            )
            return False
        if State.process_registry is not None:
            State.process_registry.pop(self.config_name, None)
        try:
            from module.application.runtime_state import RuntimeStateStore

            RuntimeStateStore(_REPOSITORY_ROOT).mark_worker_stopped(
                self.config_name,
                expected_worker_pid=(expected_worker.get("pid") if expected_worker else None),
                expected_worker_created_at=(expected_worker.get("created_at") if expected_worker else None),
                operation_id=self._runtime_operation_id,
                session_id=self._runtime_session_id,
            )
        except Exception as exc:  # noqa: BLE001 - registry остаётся источником истины.
            logger.warning(
                f"[{self.config_name}] Не удалось обновить остановленное runtime state: {exc}"
            )
        self._stop_event = None
        self._runtime_operation_id = None
        self._runtime_session_id = None
        return True

    def _thread_log_queue_handler(self) -> None:
        while self.alive:
            try:
                log = self._renderable_queue.get(timeout=1)
            except queue.Empty:
                continue
            self.renderables.append(log)
            if len(self.renderables) > self.renderables_max_length:
                self.renderables = self.renderables[self.renderables_reduce_length :]
        logger.info("Цикл обработки очереди журналов завершён")

    @property
    def alive(self) -> bool:
        with self._get_lifecycle_lock(self.config_name):
            if self._is_process_alive(self._process):
                return True
            pid, pid_verified = self._registered_pid()
            if not pid_verified:
                # При ошибке проверки registry и завершённом локальном дескрипторе
                # консервативно считаем worker остановленным, чтобы свойства alert
                # не блокировали поток журналов и отображение состояния.
                # start() дополнительно проверяет _registered_worker перед повторным запуском.
                return False
            return pid is not None

    @property
    def state(self) -> int:
        override_state = self._get_state_override()
        if override_state is not None:
            return override_state
        if self.alive:
            return 1
        elif len(self.renderables) == 0:
            return 2
        else:
            console = Console(no_color=True)
            tail = self.renderables[-8:]
            rendered_tail = []
            for renderable in tail:
                with console.capture() as capture:
                    console.print(renderable)
                rendered_tail.append(capture.get().strip())
            s = rendered_tail[-1] if rendered_tail else ""
            if (
                "Reason: Manual stop" in s
                or "Причина: ручная остановка" in s
                or "\u539f\u56e0: \u624b\u52a8\u505c\u6b62" in s
            ):
                return 2
            if (
                "Reason: Stop request" in s
                or "Причина: запрос остановки" in s
                or "\u539f\u56e0: \u505c\u6b62\u8bf7\u6c42" in s
            ):
                return 2
            if (
                "Reason: Finish" in s
                or "Причина: выполнение окончено" in s
                or "\u539f\u56e0: \u5b8c\u6210" in s
            ):
                return 2
            if "Эта версия предназначена для демонстрации" in s:
                return 2
            return 3

    @classmethod
    def get_manager(cls, config_name: str) -> "ProcessManager":
        """
        Получить менеджер процессов указанного экземпляра конфигурации и создать его при отсутствии.

        Args:
            config_name: имя экземпляра конфигурации (например, 'alas')

        Returns:
            соответствующий экземпляр ProcessManager.
        """
        with cls._managers_lock:
            if config_name not in cls._processes:
                cls._processes[config_name] = ProcessManager(config_name)
            return cls._processes[config_name]

    @classmethod
    def is_running(cls, config_name: str) -> bool:
        """Проверить, работает ли указанный экземпляр конфигурации."""
        with cls._managers_lock:
            manager = cls._processes.get(config_name)
        return manager is not None and manager.alive

    @classmethod
    def remove_manager(cls, config_name: str) -> None:
        """Удалить менеджер указанного экземпляра конфигурации."""
        with cls._managers_lock:
            cls._processes.pop(config_name, None)

    @staticmethod
    def _wait_for_runtime_state_registration(
        config_name: str,
        repository_root: str | None,
    ) -> bool:
        """Не запускать тело worker до bounded регистрации exact process identity."""
        try:
            import psutil

            from module.application.runtime_state import RuntimeStateStore

            process = psutil.Process(os.getpid())
            worker_created_at = float(process.create_time())
            state_store = RuntimeStateStore(repository_root or _REPOSITORY_ROOT)
            ready = state_store.wait_for_worker_started(
                config_name,
                worker_pid=os.getpid(),
                worker_created_at=worker_created_at,
            )
        except Exception as exc:  # noqa: BLE001 - startup gate работает fail-closed.
            logger.error(
                f"[{config_name}] Не удалось подтвердить регистрацию worker перед запуском: "
                f"{type(exc).__name__}"
            )
            return False
        if not ready:
            logger.error(
                f"[{config_name}] Регистрация worker не подтверждена за ограниченное время; тело задачи не запущено"
            )
        return ready

    @staticmethod
    def _runtime_state_heartbeat(
        config_name: str,
        repository_root: str | None,
        operation_id: str | None,
        session_id: str | None,
        stop_event: threading.Event,
    ) -> None:
        """Поддерживать свежесть runtime state только для текущего worker."""

        try:
            import psutil

            from module.application.runtime_state import (
                RuntimeStateError,
                RuntimeStateStore,
            )

            process = psutil.Process(os.getpid())
            worker_created_at = float(process.create_time())
            state_store = RuntimeStateStore(repository_root or _REPOSITORY_ROOT)
        except Exception as exc:  # noqa: BLE001 - heartbeat не должен менять worker lifecycle.
            logger.warning(
                f"[{config_name}] Не удалось подготовить heartbeat runtime state: {type(exc).__name__}"
            )
            return

        while not stop_event.wait(_RUNTIME_STATE_HEARTBEAT_SECONDS):
            try:
                snapshot = state_store.refresh_worker_heartbeat(
                    config_name,
                    worker_pid=os.getpid(),
                    worker_created_at=worker_created_at,
                    operation_id=operation_id,
                    session_id=session_id,
                )
            except RuntimeStateError as exc:
                if exc.code == "RUNTIME_STATE_STALE_WRITE":
                    return
                logger.warning(
                    f"[{config_name}] Heartbeat runtime state остановлен: {exc.code}"
                )
                return
            except Exception as exc:  # noqa: BLE001 - неизвестное состояние требует fail-closed read.
                logger.warning(
                    f"[{config_name}] Heartbeat runtime state не подтверждён: {type(exc).__name__}"
                )
                continue
            if snapshot is None or snapshot.worker_running is not True:
                return

    @staticmethod
    def run_process(
        config_name,
        func: str,
        q: queue.Queue[ConsoleRenderable],
        e: object | None = None,
        repository_root: str | None = None,
        operation_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        os.environ["AZURPILOT_REPOSITORY_ROOT"] = str(
            Path(repository_root or _REPOSITORY_ROOT).resolve()
        )
        if session_id:
            os.environ["AZURPILOT_DEV_SESSION_ID"] = session_id
        else:
            os.environ.pop("AZURPILOT_DEV_SESSION_ID", None)
        if operation_id:
            os.environ["AZURPILOT_RUNTIME_OPERATION_ID"] = operation_id
        else:
            os.environ.pop("AZURPILOT_RUNTIME_OPERATION_ID", None)
        if not ProcessManager._wait_for_runtime_state_registration(
            config_name,
            repository_root,
        ):
            return
        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=ProcessManager._runtime_state_heartbeat,
            args=(
                config_name,
                repository_root,
                operation_id,
                session_id,
                heartbeat_stop,
            ),
            name=f"AzurPilot-runtime-heartbeat-{config_name}",
            daemon=True,
        )
        heartbeat.start()
        try:
            ProcessManager._run_process_body(config_name, func, q, e)
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=_RUNTIME_STATE_HEARTBEAT_JOIN_SECONDS)
            try:
                import psutil

                from module.application.runtime_state import RuntimeStateStore

                created_at = float(psutil.Process(os.getpid()).create_time())
                RuntimeStateStore(repository_root or _REPOSITORY_ROOT).mark_worker_stopped(
                    config_name,
                    expected_worker_pid=os.getpid(),
                    expected_worker_created_at=created_at,
                    operation_id=operation_id,
                    session_id=session_id,
                )
            except Exception:
                # Registry/owner остаётся источником истины; дочерний cleanup
                # не может самостоятельно изменить ownership.
                pass

    @staticmethod
    def _run_process_body(
        config_name,
        func: str,
        q: queue.Queue[ConsoleRenderable],
        e: object | None = None,
    ) -> None:
        import sys

        if sys.platform != "win32":
            import resource

            try:
                _soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
                _target = (
                    65536 if _hard == resource.RLIM_INFINITY else min(65536, _hard)
                )
                if _soft < _target:
                    resource.setrlimit(resource.RLIMIT_NOFILE, (_target, _hard))
            except Exception:
                pass
        parser = argparse.ArgumentParser()
        parser.add_argument(
            "--electron",
            action="store_true",
                help="Включается при запуске из клиента Electron.",
        )
        args, _ = parser.parse_known_args()
        State.electron = args.electron

        # Инициализировать журнал.
        set_file_logger(name=config_name)
        if State.electron:
            # См. https://github.com/LmeSzinc/AzurLaneAutoScript/issues/2051.
            logger.info("[WebUI] Обнаружена среда Electron; обработчик стандартного вывода удалён")
            from module.logger import console_hdlr

            logger.removeHandler(console_hdlr)
        set_func_logger(func=q.put)

        if os.environ.get("DEMO") == "1":
            logger.info("[WebUI-процесс] Тестовая запись 3")
            time.sleep(1)
            logger.info("[WebUI-процесс] Тестовая запись 2")
            time.sleep(1)
            logger.info("[WebUI-процесс] Тестовая запись 1")
            time.sleep(1)
            logger.info("[WebUI] Эта версия предназначена для демонстрации")
            return

        from module.config.config import AzurLaneConfig

        # Удалить поддельный модуль PIL: дочернему процессу нужен настоящий PIL.
        remove_fake_pil_module()

        # Установить переменную окружения, чтобы предварительно загружаемые модули
        # (например, al_ocr.py) могли заранее прочитать конфигурацию.
        os.environ["ALAS_CONFIG_NAME"] = config_name

        if e is not None:
            AzurLaneConfig.stop_event = e
        try:
            # Запустить AzurPilot.
            if func == "alas":
                from alas import AzurLaneAutoScript

                if e is not None:
                    AzurLaneAutoScript.stop_event = e
                AzurLaneAutoScript(config_name=config_name).loop()
            elif func in get_available_func():
                from alas import AzurLaneAutoScript

                AzurLaneAutoScript(config_name=config_name).run(
                    inflection.underscore(func), skip_first_screenshot=True
                )
            elif func in get_available_mod():
                mod = load_mod(func)

                if mod is None:
                    logger.critical(f"[WebUI] Не удалось загрузить функциональный модуль: {func}")
                    return

                if e is not None:
                    mod.set_stop_event(e)
                mod.loop(config_name)
            elif func in get_available_mod_func():
                getattr(load_mod(get_func_mod(func)), inflection.underscore(func))(
                    config_name
                )
            else:
                logger.critical(
                    f"[WebUI] Функциональный модуль не найден: {func}"
                )
            if e is not None and e.is_set():
                logger.info(f"[{config_name}] завершён. Причина: запрос остановки\n")
            else:
                logger.info(f"[{config_name}] завершён. Причина: выполнение окончено\n")
        except Exception as ex:
            logger.exception(f"[{config_name}] Необработанная ошибка рабочего процесса: {ex}")

    @classmethod
    def running_instances(cls) -> List["ProcessManager"]:
        with cls._managers_lock:
            names = set(cls._processes)
        if State.process_registry is not None:
            names.update(State.process_registry.keys())
        return [cls.get_manager(name) for name in names if cls.get_manager(name).alive]

    @staticmethod
    def restart_processes(
        instances: Sequence[Union["ProcessManager", str]] | None = None,
        ev: threading.Event | None = None,
    ) -> None:
        """
        Перезапустить указанные экземпляры AzurPilot после перезагрузки WebUI.

        Args:
            instances: список экземпляров для перезапуска; элементы являются ProcessManager
                или строками имён конфигурации.
            ev: необязательное общее событие остановки, передаваемое перезапускаемым процессам.
        """
        logger.hr("[WebUI-процессы] Перезапуск AzurPilot")
        from module.application.runtime_state import RuntimeStateError

        # Загрузить MOD_CONFIG_DICT.
        list_mod_instance()

        if instances is None:
            instances = []

        _instances: set[ProcessManager] = set()

        for instance in instances:
            if isinstance(instance, str):
                _instances.add(ProcessManager.get_manager(instance))
            elif isinstance(instance, ProcessManager):
                _instances.add(instance)

        try:
            with open("./config/reloadalas", mode="r", encoding="utf-8") as f:
                for line in f.readlines():
                    line = line.strip()
                    _instances.add(ProcessManager.get_manager(line))
        except FileNotFoundError:
            pass

        for process in _instances:
            logger.info(f"Запускается [{process.config_name}]")
            try:
                process.start(func=get_config_mod(process.config_name), ev=ev)
            except RuntimeStateError as exc:
                logger.error(
                    f"[{process.config_name}] Не удалось запустить worker из-за runtime state: {exc}"
                )

        try:
            os.remove("./config/reloadalas")
        except:
            pass
        logger.info("[WebUI-процессы] Запуск AzurPilot завершён")
