import errno
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from multiprocessing import Event, Process, Queue, set_start_method
from typing import Optional

if sys.platform != "win32":
    import resource
    try:
        _soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        _target = 65536 if _hard == resource.RLIM_INFINITY else min(65536, _hard)
        if _soft < _target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (_target, _hard))
    except Exception:
        pass

from deploy.uv import (
    DEPENDENCY_SYNC_TIMEOUT,
    dependency_sync_service,
    log_command_output,
    redact_sensitive_text,
)
from module.logger import logger
from module.webui.setting import (
    State,
    clear_dependency_sync_pending,
    is_dependency_sync_pending,
)
from module.webui import worker_registry


WEBUI_READY_TIMEOUT = 120
WEBUI_START_RETRY_LIMIT = 3
WEBUI_RUNTIME_RETRY_LIMIT = 3
WEBUI_STABLE_RUNTIME = 60
DEPENDENCY_SYNC_START_RETRY_LIMIT = 3
DEPENDENCY_SYNC_RESPONSE_TIMEOUT = DEPENDENCY_SYNC_TIMEOUT + 60


def _is_ipv6_unavailable_error(exc: OSError) -> bool:
    """判断 IPv6 地址族在当前系统中是否不可用。"""
    errno_values = {
        errno.EAFNOSUPPORT,
        errno.EPROTONOSUPPORT,
        errno.EADDRNOTAVAIL,
        getattr(errno, "ENODEV", -1),
        getattr(errno, "ENOPROTOOPT", -1),
        getattr(errno, "EOPNOTSUPP", -1),
    }
    winerror_values = {10042, 10043, 10045, 10047, 10049}
    return exc.errno in errno_values or getattr(exc, "winerror", None) in winerror_values


def _create_dual_stack_sockets(
    port: int,
    backlog: int = 2048,
    *,
    allow_ipv6_fallback: bool = False,
) -> list[socket.socket]:
    """创建同端口的 IPv4/IPv6 WebUI socket，并可降级为 IPv4。"""
    sockets = []
    listen_port = port
    try:
        for family, address in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
            listener = None
            try:
                listener = socket.socket(family, socket.SOCK_STREAM)
                if os.name != "nt":
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if family == socket.AF_INET6:
                    listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                listener.bind((address, listen_port))
                listener.listen(backlog)
                listener.setblocking(False)
            except OSError as exc:
                if listener is not None:
                    listener.close()
                if (
                    family == socket.AF_INET6
                    and allow_ipv6_fallback
                    and _is_ipv6_unavailable_error(exc)
                ):
                    break
                raise
            sockets.append(listener)
            if listen_port == 0:
                listen_port = listener.getsockname()[1]
        return sockets
    except Exception:
        for listener in sockets:
            listener.close()
        raise


def _watch_server_started(server, ready_event: Event) -> None:
    """在 Uvicorn 完成监听后通知父进程。"""
    while not server.started:
        if server.should_exit or server.force_exit:
            return
        time.sleep(0.1)
    ready_event.set()


def _run_uvicorn_server(config, ready_event: Optional[Event] = None, sockets=None) -> None:
    """运行 Uvicorn，并在端口实际监听后发送就绪信号。"""
    import uvicorn

    server = uvicorn.Server(config)
    if ready_event is not None:
        threading.Thread(
            target=_watch_server_started,
            args=(server, ready_event),
            daemon=True,
            name="webui-ready-watcher",
        ).start()
    server.run(sockets=sockets)


def func(
    ev: Optional[Event],
    dependency_sync_event: Optional[Event] = None,
    ready_event: Optional[Event] = None,
):
    """
    主函数：运行Web服务。

    Args:
        ev: 可选的重启事件，用于热重载功能
        dependency_sync_event: 请求父进程同步依赖的事件
        ready_event: Uvicorn 完成监听后通知父进程的事件
    """
    import argparse
    import asyncio
    import uvicorn

    # 平台特定的asyncio配置
    if sys.platform == "darwin":
        # macOS: 禁用fork安全检查以避免Mach端口冲突
        os.environ["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] = "YES"
        asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
    elif sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    State.restart_event = ev
    State.dependency_sync_event = dependency_sync_event

    # 解析命令行参数
    parser = argparse.ArgumentParser(description="Веб-служба AzurPilot")
    parser.add_argument(
        "--host",
        type=str,
        help="Адрес прослушивания. По умолчанию используется WebuiHost из настроек развёртывания",
    )
    parser.add_argument(
        "-p",
        "--port",
        type=int,
        help="Порт прослушивания. По умолчанию используется WebuiPort из настроек развёртывания",
    )
    parser.add_argument(
        "-k", "--key", type=str, help="Пароль AzurPilot. По умолчанию пароль не используется"
    )
    parser.add_argument(
        "--cdn",
        action="store_true",
        help="Загружать статические файлы PyWebIO (CSS, JS) через CDN jsDelivr. По умолчанию используются локальные файлы",
    )
    parser.add_argument(
        "--electron", action="store_true", help="Запуск из клиента Electron"
    )
    parser.add_argument(
        "--ssl-key", dest="ssl_key", type=str, help="Путь к ключу SSL для HTTPS"
    )
    parser.add_argument(
        "--ssl-cert", type=str, help="Путь к сертификату SSL для HTTPS"
    )
    parser.add_argument(
        "--run",
        nargs="+",
        type=str,
        help="Запустить при старте указанные конфигурации AzurPilot",
    )
    args, _ = parser.parse_known_args()

    # 配置服务器设置
    host = args.host or State.deploy_config.WebuiHost or "0.0.0.0"
    port = args.port or int(State.deploy_config.WebuiPort) or 25548
    ssl_key = args.ssl_key or State.deploy_config.WebuiSSLKey
    ssl_cert = args.ssl_cert or State.deploy_config.WebuiSSLCert
    ssl = ssl_key is not None and ssl_cert is not None
    State.electron = args.electron
    State.webui_host = host

    # 记录启动器配置
    logger.hr("КОНФИГУРАЦИЯ ЗАПУСКА")
    logger.attr("Адрес", host)
    logger.attr("Порт", port)
    logger.attr("SSL", ssl)
    logger.attr("Electron", args.electron)
    logger.attr("Перезапуск", ev is not None)

    # Electron客户端特定处理
    if State.electron:
        # https://github.com/LmeSzinc/AzurLaneAutoScript/issues/2051
        logger.info("[GUI] Обнаружен Electron; обработчик вывода в stdout удалён")
        from module.logger import console_hdlr
        logger.removeHandler(console_hdlr)

    # 验证SSL配置
    if ssl_cert is None and ssl_key is not None:
        logger.error("[GUI] Указан ключ SSL, но не указан сертификат. Укажите одновременно ключ и сертификат SSL.")
    elif ssl_key is None and ssl_cert is not None:
        logger.error("[GUI] Указан сертификат SSL, но не указан ключ. Укажите одновременно ключ и сертификат SSL.")

    # 通配地址显式创建两个 socket，避免 Windows 将 IPv6 wildcard 作为仅 IPv6 监听。
    try:
        uvicorn_options = {
            "host": host,
            "port": port,
            "factory": True,
        }
        if ssl:
            uvicorn_options.update(
                ssl_keyfile=ssl_key,
                ssl_certfile=ssl_cert,
            )

        if host in ("0.0.0.0", "::", "[::]"):
            if host in ("::", "[::]"):
                uvicorn_options["host"] = "::"
            config = uvicorn.Config("module.webui.app:app", **uvicorn_options)
            sockets = _create_dual_stack_sockets(
                port,
                backlog=config.backlog,
                allow_ipv6_fallback=host == "0.0.0.0",
            )
            try:
                if len(sockets) == 2:
                    logger.info(
                        f"[GUI] WebUI прослушивает IPv4 0.0.0.0:{port} и IPv6 [::]:{port}"
                    )
                else:
                    logger.warning(
                        f"[GUI] IPv6 недоступен в системе; WebUI прослушивает только IPv4 0.0.0.0:{port}"
                    )
                _run_uvicorn_server(config, ready_event=ready_event, sockets=sockets)
            finally:
                for listener in sockets:
                    listener.close()
        else:
            config = uvicorn.Config("module.webui.app:app", **uvicorn_options)
            _run_uvicorn_server(config, ready_event=ready_event)
    except Exception as e:
        logger.exception_context(
            title='Не удалось запустить службу WebUI',
            exc=e,
            impact='Процесс WebUI завершится, управление AzurPilot будет недоступно.',
            action='Проверьте доступность порта, соответствие сертификата и ключа SSL и установку зависимостей через uv sync --frozen.',
            level=50,
        )
        raise


def _stop_process(process, timeout=5) -> bool:
    """
    安全停止子进程，采用逐级升级的终止策略。

    先尝试 terminate()，超时后升级为 kill() 强制终止。

    Args:
        process: 待停止的 multiprocessing.Process 实例
        timeout: 等待进程优雅退出的超时时间（秒），默认 5

    Returns:
        bool: 子进程是否已确认退出。
    """
    if not process:
        return True
    try:
        alive = process.is_alive()
    except (OSError, ValueError, AssertionError):
        return True
    if not alive:
        try:
            process.join(timeout=0)
        except (OSError, ValueError, AssertionError):
            pass
        return True

    logger.info(f"[GUI] Остановка процесса службы (PID: {process.pid})...")
    try:
        process.terminate()
    except (OSError, ValueError, AssertionError) as exc:
        logger.warning(f"[GUI] Не удалось завершить процесс службы (PID: {process.pid}): {exc}")
    process.join(timeout=timeout)

    if process.is_alive():
        logger.warning(f"[GUI] Процесс службы (PID: {process.pid}) не завершился за отведённое время; выполняется принудительная остановка...")
        try:
            process.kill()
        except (OSError, ValueError, AssertionError) as exc:
            logger.warning(f"[GUI] Не удалось принудительно завершить процесс службы (PID: {process.pid}): {exc}")
        process.join(timeout=3)

    stopped = not process.is_alive()
    if not stopped:
        logger.error(f"[GUI] Процесс службы (PID: {process.pid}) всё ещё выполняется; перезапуск отменён во избежание конфликта порта")
    return stopped


def _wait_for_webui_ready(process, ready_event: Event, timeout=WEBUI_READY_TIMEOUT) -> bool:
    """等待 WebUI 完成 ASGI 启动和 socket 监听。"""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if ready_event.wait(min(0.2, remaining)):
            return process.is_alive()
        if not process.is_alive():
            return False


def _stop_process_tree(process, name: str) -> bool:
    """终止指定进程及其子树，并确认根进程已退出。"""
    if not process:
        return True
    try:
        alive = process.is_alive()
    except (OSError, ValueError, AssertionError):
        return True
    if not alive:
        try:
            process.join(timeout=0)
        except (OSError, ValueError, AssertionError):
            pass
        return True

    pid = process.pid
    logger.warning(f"[GUI] Принудительное завершение дерева процессов «{name}» (PID: {pid})...")
    tree_terminated = True
    child_processes = []
    psutil_module = None
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=5,
            )
            tree_terminated = result.returncode == 0
            if not tree_terminated and process.is_alive():
                logger.warning(f"[GUI] taskkill не завершил процесс «{name}» (PID: {pid})")
                try:
                    process.kill()
                except (OSError, ValueError, AssertionError) as exc:
                    logger.warning(f"[GUI] Не удалось принудительно завершить процесс «{name}» (PID: {pid}): {exc}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning(f"[GUI] Не удалось завершить дерево процессов «{name}»: {exc}")
            tree_terminated = False
    else:
        try:
            import psutil

            psutil_module = psutil
            parent = psutil.Process(pid)
            child_processes = parent.children(recursive=True)
            for child in reversed(child_processes):
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
        except ImportError:
            logger.warning(f"[GUI] psutil недоступен; невозможно подтвердить завершение дочерних процессов «{name}»")
            tree_terminated = False
        except psutil.NoSuchProcess:
            # 根进程可能在 is_alive() 检查后自然退出；此时与前置已退出分支等价。
            logger.info(f"[GUI] Корневой процесс «{name}» завершился до перечисления дочерних процессов (PID: {pid})")
        except Exception as exc:
            logger.warning(f"[GUI] Не удалось перечислить дочерние процессы «{name}»: {exc}")
            tree_terminated = False
        try:
            process.kill()
        except (OSError, ValueError, AssertionError) as exc:
            logger.warning(f"[GUI] Не удалось принудительно завершить процесс «{name}» (PID: {pid}): {exc}")
            tree_terminated = False

    process.join(timeout=3)
    stopped = not process.is_alive()
    if os.name != "nt" and psutil_module is not None and child_processes:
        try:
            _, alive_children = psutil_module.wait_procs(child_processes, timeout=3)
        except Exception as exc:
            logger.warning(f"[GUI] Не удалось дождаться завершения дочерних процессов «{name}»: {exc}")
            tree_terminated = False
        else:
            if alive_children:
                child_pids = ", ".join(
                    str(getattr(child, "pid", "неизвестно")) for child in alive_children
                )
                logger.error(f"[GUI] Дочерние процессы «{name}» всё ещё выполняются (PID: {child_pids})")
                tree_terminated = False
    if os.name == "nt" and stopped and not tree_terminated:
        # taskkill 可能与子进程自然退出交错；根进程已确认退出时不应阻断重启。
        logger.warning(
            f"[GUI] taskkill не сообщил об успешном завершении, но корневой процесс «{name}» уже остановлен (PID: {pid})"
        )
        tree_terminated = True
    if not stopped or not tree_terminated:
        logger.error(f"[GUI] Дерево процессов «{name}» всё ещё выполняется (PID: {pid})")
    return stopped and tree_terminated


def _wait_for_registered_worker_exit(
    pid: int,
    name: str,
    record: dict,
    timeout: float = 3,
) -> bool:
    """等待登记 worker 退出，并拒绝 PID 已复用的记录。"""
    deadline = time.monotonic() + timeout
    while True:
        try:
            matches = worker_registry.process_matches(record)
        except RuntimeError as exc:
            logger.error(f"[GUI] Не удалось подтвердить завершение worker «{name}» (PID: {pid}): {exc}")
            return False
        if matches is None:
            return True
        if not matches:
            logger.error(
                f"[GUI] PID worker был повторно использован; завершение неизвестного процесса отклонено: {name} (PID: {pid})"
            )
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.error(f"[GUI] Превышено время ожидания завершения worker «{name}» (PID: {pid})")
            return False
        time.sleep(min(0.1, remaining))


def _stop_registered_worker(pid: int, name: str, record: dict) -> bool:
    """终止登记的 worker，并验证 PID 没有被系统复用。"""
    try:
        matches = worker_registry.process_matches(record)
    except RuntimeError as exc:
        logger.error(f"[GUI] Не удалось подтвердить идентичность worker «{name}» (PID: {pid}): {exc}")
        return False
    if matches is None:
        return True
    if not matches:
        logger.error(
            f"[GUI] PID worker был повторно использован; завершение неизвестного процесса отклонено: {name} (PID: {pid})"
        )
        return False

    if os.name == "nt":
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning(f"[GUI] Не удалось завершить worker «{name}» (PID: {pid}): {exc}")
            return False
        if result.returncode != 0:
            logger.warning(
                f"[GUI] taskkill при завершении worker «{name}» (PID: {pid}) вернул код {result.returncode}"
            )
    else:
        try:
            import psutil
        except ImportError:
            logger.warning(f"[GUI] psutil недоступен; невозможно завершить worker «{name}» (PID: {pid})")
            return False

        try:
            parent = psutil.Process(pid)
            children = parent.children(recursive=True)
            for child in reversed(children):
                child.kill()
            parent.kill()
            alive = [parent, *children]
            deadline = time.monotonic() + 3
            while alive:
                remaining = []
                for process in alive:
                    try:
                        if process.status() != psutil.STATUS_ZOMBIE:
                            remaining.append(process)
                    except psutil.NoSuchProcess:
                        continue
                alive = remaining
                if not alive or time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
            if alive:
                logger.error(f"[GUI] worker «{name}» (PID: {pid}) всё ещё выполняется")
                return False
        except psutil.NoSuchProcess:
            return True
        except Exception as exc:
            logger.warning(f"[GUI] Не удалось завершить worker «{name}» (PID: {pid}): {exc}")
            return False

    return _wait_for_registered_worker_exit(pid, name, record)


def _stop_registered_workers(
    owner_pid: int | None,
    discard_reused: bool = False,
) -> bool:
    """回收指定 WebUI 所登记的 worker，覆盖根进程已异常退出的场景。"""
    if owner_pid is None:
        return True
    try:
        workers = worker_registry.get_workers(owner_pid)
    except RuntimeError as exc:
        logger.error(f"[GUI] Не удалось прочитать реестр worker WebUI: {exc}")
        return False

    stopped = True
    for name, record in workers.items():
        try:
            pid = int(record["pid"])
            matches = worker_registry.process_matches(record)
        except (KeyError, TypeError, ValueError, RuntimeError) as exc:
            logger.error(f"[GUI] Недопустимая запись worker ({name}): {exc}")
            stopped = False
            continue
        if matches is None:
            continue
        if not matches:
            if discard_reused:
                logger.warning(
                    f"[GUI] PID worker был повторно использован; устаревшая запись прежнего владельца отброшена: {name} (PID: {pid})"
                )
            else:
                logger.error(
                    f"[GUI] PID worker был повторно использован; завершение неизвестного процесса отклонено: {name} (PID: {pid})"
                )
                stopped = False
            continue
        stopped = _stop_registered_worker(pid, name, record) and stopped

    if stopped:
        try:
            worker_registry.clear_owner(owner_pid)
        except RuntimeError as exc:
            logger.error(f"[GUI] Не удалось очистить реестр worker WebUI: {exc}")
            return False
    return stopped


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _recover_orphaned_workers() -> bool:
    """启动前回收上次异常退出的 WebUI worker。"""
    try:
        owner_record = worker_registry.get_owner_record()
    except RuntimeError as exc:
        logger.error(f"[GUI] Не удалось прочитать прежний реестр worker WebUI: {exc}")
        return False
    if owner_record is None:
        return True

    owner_pid = owner_record["pid"]
    try:
        owner_matches = worker_registry.process_matches(owner_record)
    except RuntimeError as exc:
        # 兼容旧登记文件：没有创建时间时，只有确认 PID 已消失才能安全回收。
        if not _pid_exists(owner_pid):
            logger.warning(
                f"[GUI] В записи прежнего владельца WebUI отсутствуют данные идентификации; выполняется очистка завершённого экземпляра (PID: {owner_pid})"
            )
            return _stop_registered_workers(owner_pid, discard_reused=True)
        logger.error(
            f"[GUI] Не удалось проверить прежнего владельца WebUI (PID: {owner_pid}): {exc}; запуск второго WebUI отклонён"
        )
        return False

    if owner_matches is True:
        logger.error(
            f"[GUI] Обнаружен работающий владелец WebUI (PID: {owner_pid}); запуск второго WebUI отклонён"
        )
        return False
    if owner_matches is False:
        logger.warning(
            f"[GUI] PID прежнего владельца WebUI был повторно использован; выполняется очистка зарегистрированных worker (PID: {owner_pid})"
        )
    else:
        logger.warning(f"[GUI] Очистка worker после аварийного завершения предыдущего WebUI (PID: {owner_pid})")
    return _stop_registered_workers(owner_pid, discard_reused=True)


def _stop_dependency_sync_service_tree(process) -> bool:
    """终止卡住的依赖同步服务及其 uv 子进程。"""
    return _stop_process_tree(process, "служба синхронизации зависимостей")


def _stop_webui_process_tree(process) -> bool:
    """终止 WebUI 及其 AzurPilot worker 子进程，避免重启后重复控制设备。"""
    root_stopped = _stop_process_tree(process, "WebUI")
    if not root_stopped:
        # 根 WebUI 仍可能继续创建或管理 worker，不能清除其登记。
        return False
    owner_pid = getattr(process, "pid", None) if process is not None else None
    workers_stopped = _stop_registered_workers(owner_pid, discard_reused=True)
    return root_stopped and workers_stopped


def _start_dependency_sync_service():
    """启动空闲的依赖同步服务，避免 WebUI 进程修改自身环境。"""
    request_queue = Queue()
    response_queue = Queue()
    process = Process(
        target=dependency_sync_service,
        args=(request_queue, response_queue),
        daemon=True,
        name="dependency-sync",
    )
    process.start()
    logger.info(f"[GUI] Служба синхронизации зависимостей запущена (PID: {process.pid})")
    return process, request_queue, response_queue


def _start_dependency_sync_service_with_retry():
    """有限重试启动依赖同步服务，避免启动器因单次进程错误直接崩溃。"""
    for attempt in range(1, DEPENDENCY_SYNC_START_RETRY_LIMIT + 1):
        try:
            return _start_dependency_sync_service()
        except Exception as exc:
            logger.exception_context(
                title='Не удалось запустить службу синхронизации зависимостей',
                exc=exc,
                impact='Текущая WebUI не может безопасно обновить зависимости автоматически.',
                action='Проверьте права управления процессами и окружение Python; лаунчер выполнит ограниченное число повторов.',
                level=50,
            )
            if attempt < DEPENDENCY_SYNC_START_RETRY_LIMIT:
                logger.warning(
                    f"[GUI] Не удалось запустить службу синхронизации зависимостей; повтор через {attempt} с ({attempt}/{DEPENDENCY_SYNC_START_RETRY_LIMIT})"
                )
                time.sleep(attempt)
    return None


def _stop_dependency_sync_service(process, request_queue) -> bool:
    """停止依赖同步服务，确保启动器关闭时不遗留后端进程。"""
    if not process:
        return True
    if not process.is_alive():
        try:
            process.join(timeout=0)
        except (OSError, ValueError, AssertionError):
            pass
        return True

    try:
        request_queue.put("shutdown")
        process.join(timeout=5)
    except Exception as exc:
        logger.warning(f"[GUI] Не удалось остановить службу синхронизации зависимостей: {exc}")

    if process.is_alive():
        return _stop_dependency_sync_service_tree(process)
    return True


def _sync_dependencies(
    process,
    request_queue,
    response_queue,
    timeout=DEPENDENCY_SYNC_RESPONSE_TIMEOUT,
) -> bool:
    """向独立服务请求同步，并将完整 uv 输出写入 GUI 日志。"""
    logger.hr("ОБНОВЛЕНИЕ ЗАВИСИМОСТЕЙ", 0)
    if not process or not process.is_alive():
        logger.critical("Служба синхронизации зависимостей не запущена")
        return False

    try:
        request_queue.put("sync")
    except (OSError, EOFError, ValueError, queue.Full) as exc:
        logger.critical(f"Не удалось отправить запрос синхронизации зависимостей; WebUI не будет перезапущена: {exc}")
        return False
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.critical(f"Синхронизация зависимостей не завершилась за {timeout} с; WebUI не будет перезапущена")
            return False
        try:
            result = response_queue.get(timeout=min(1, remaining))
        except queue.Empty:
            if not process.is_alive():
                logger.critical("Служба синхронизации зависимостей неожиданно завершилась")
                return False
            continue
        except (OSError, EOFError, ValueError) as exc:
            logger.critical(f"Ошибка связи со службой синхронизации зависимостей; WebUI не будет перезапущена: {exc}")
            return False

        command = result.get("command") or []
        if command:
            logger.info(f"Команда: {redact_sensitive_text(command)}")
        log_command_output(logger, result.get("output", ""))
        if result.get("success"):
            logger.info("Синхронизация зависимостей завершена успешно")
            return True

        error = redact_sensitive_text(result.get("error", "неизвестная ошибка"))
        logger.critical(f"Команда uv sync завершилась с ошибкой: {error}")
        return False


def _complete_pending_dependency_sync(
    process,
    request_queue,
    response_queue,
    *,
    force: bool = False,
) -> bool:
    """完成更新遗留的依赖同步，并仅在成功后清除持久化标记。"""
    try:
        pending = is_dependency_sync_pending()
    except OSError as exc:
        logger.critical(f"Не удалось прочитать состояние ожидающей синхронизации зависимостей; WebUI не будет запущена: {exc}")
        return False

    if not pending and not force:
        return True

    if pending:
        logger.warning("Обнаружена незавершённая синхронизация зависимостей; она будет продолжена до запуска WebUI")
    if not _sync_dependencies(process, request_queue, response_queue):
        return False

    if pending:
        try:
            clear_dependency_sync_pending()
        except OSError as exc:
            logger.critical(f"Не удалось очистить состояние ожидающей синхронизации зависимостей; WebUI не будет запущена: {exc}")
            return False
    return True


def _prepare_dependency_sync_before_webui_start(
    service,
    request_queue,
    response_queue,
    *,
    force: bool = False,
):
    """在创建 WebUI 前完成必要的依赖同步，失败时拒绝启动子进程。"""
    try:
        pending = is_dependency_sync_pending()
    except OSError as exc:
        logger.error_context(
            title="Не удалось прочитать состояние синхронизации зависимостей перед запуском",
            exc=exc,
            impact="Невозможно подтвердить соответствие окружения Python обновлённому коду; WebUI не будет запущена.",
            action="Проверьте права чтения и записи каталога config, затем перезапустите приложение.",
            level=50,
        )
        return False, service, request_queue, response_queue

    sync_required = pending or force
    if not sync_required:
        return True, service, request_queue, response_queue

    if service is not None:
        # 更新后必须使用新源码创建同步服务，不能复用旧环境中的服务进程。
        if not _stop_dependency_sync_service(service, request_queue):
            logger.error_context(
                title="Не удалось остановить службу синхронизации зависимостей",
                reason="Прежняя служба синхронизации зависимостей или её дочерний процесс uv всё ещё выполняется.",
                impact="Продолжение синхронизации может привести к одновременному изменению окружения Python; WebUI не будет запущена.",
                action="Завершите оставшиеся процессы dependency-sync и uv, затем перезапустите приложение.",
                level=50,
            )
            return False, service, request_queue, response_queue
        service = None
        request_queue = None
        response_queue = None

    service_data = _start_dependency_sync_service_with_retry()
    if service_data is None:
        logger.error_context(
            title="Не удалось запустить службу синхронизации зависимостей",
            reason="Несколько последовательных попыток создать дочерний процесс синхронизации зависимостей завершились ошибкой.",
            impact="Окружение необходимо синхронизировать; WebUI не запущена во избежание работы с несовместимыми зависимостями.",
            action="Проверьте права управления системными процессами и окружение Python, затем перезапустите приложение.",
            level=50,
        )
        return False, None, None, None

    service, request_queue, response_queue = service_data
    if not _complete_pending_dependency_sync(
        service,
        request_queue,
        response_queue,
        force=sync_required,
    ):
        logger.error_context(
            title="Синхронизация зависимостей перед созданием WebUI завершилась ошибкой",
            reason="Обнаружено обновление или ожидающее состояние синхронизации, но синхронизация не завершилась.",
            impact="Во избежание запуска WebUI в несовместимом окружении Python родительский процесс завершится.",
            action="Проверьте вывод uv sync, права доступа к диску и окружение Python, затем перезапустите приложение.",
            level=50,
        )
        return False, service, request_queue, response_queue
    return True, service, request_queue, response_queue


def run_webui_supervisor() -> None:
    """监督热重载 WebUI 子进程及其独立依赖同步服务。"""
    should_exit = False
    process = None
    service = None
    service_request_queue = None
    service_response_queue = None
    startup_failures = 0
    runtime_failures = 0
    force_dependency_sync = False
    if not _recover_orphaned_workers():
        return
    try:
        while not should_exit:
            (
                ready_to_start,
                service,
                service_request_queue,
                service_response_queue,
            ) = _prepare_dependency_sync_before_webui_start(
                service,
                service_request_queue,
                service_response_queue,
                force=force_dependency_sync,
            )
            if not ready_to_start:
                should_exit = True
                break
            force_dependency_sync = False

            event = Event()
            dependency_sync_event = Event()
            ready_event = Event()
            process = None
            try:
                process = Process(
                    target=func,
                    args=(event, dependency_sync_event, ready_event),
                    name="gui",
                )
                process.start()
            except Exception as exc:
                _stop_webui_process_tree(process)
                startup_failures += 1
                logger.exception_context(
                    title='Не удалось запустить дочерний процесс WebUI',
                    exc=exc,
                    impact='WebUI сейчас недоступна.',
                    action='Проверьте права управления процессами и системные ресурсы; запуск будет повторён ограниченное число раз.',
                    level=50,
                )
                if startup_failures >= WEBUI_START_RETRY_LIMIT:
                    should_exit = True
                else:
                    time.sleep(startup_failures)
                continue
            logger.info(f"[GUI] Запуск службы WebUI AzurPilot (PID: {process.pid})")

            try:
                ready = _wait_for_webui_ready(process, ready_event)
            except KeyboardInterrupt:
                logger.info("[GUI] Получен KeyboardInterrupt; выполняется завершение...")
                should_exit = True
                _stop_webui_process_tree(process)
                break

            if not ready:
                stopped = _stop_webui_process_tree(process)
                startup_failures += 1
                if not stopped:
                    logger.error_context(
                        title="Дочерний процесс WebUI не запустился и не был завершён",
                        reason="Дочерний процесс не начал прослушивание за отведённое время и остался активен после попытки завершения.",
                        impact="Запуск нового WebUI приведёт к конфликту порта.",
                        action="Завершите оставшийся дочерний процесс gui.py, затем перезапустите приложение.",
                        level=50,
                    )
                    should_exit = True
                elif startup_failures >= WEBUI_START_RETRY_LIMIT:
                    logger.error_context(
                        title="Дочерний процесс WebUI не завершил запуск",
                        reason=f"После {startup_failures} последовательных попыток прослушивание не началось за {WEBUI_READY_TIMEOUT} с.",
                        impact="WebUI не запущена; родительский процесс завершится.",
                        action="Проверьте занятость порта, журналы WebUI и окружение Python, затем перезапустите приложение.",
                        level=50,
                    )
                    should_exit = True
                else:
                    logger.warning(
                        f"[GUI] WebUI не готова; повтор через {startup_failures} с ({startup_failures}/{WEBUI_START_RETRY_LIMIT})"
                    )
                    time.sleep(startup_failures)
                continue

            startup_failures = 0
            ready_at = time.monotonic()
            logger.info(f"[GUI] Служба WebUI готова (PID: {process.pid})")

            while not should_exit:
                try:
                    # 等待重启事件，超时1秒
                    restart_triggered = event.wait(1)
                except KeyboardInterrupt:
                    logger.info("[GUI] Получен KeyboardInterrupt; выполняется завершение...")
                    should_exit = True
                    break
                except Exception as e:
                    logger.exception_context(
                        title='Ошибка обработки события перезапуска WebUI',
                        exc=e,
                        impact='WebUI прекратит горячую перезагрузку и завершится.',
                        action='Проверьте состояние дочернего процесса WebUI и системные права управления процессами.',
                        level=50,
                    )
                    should_exit = True
                    break

                if restart_triggered:
                    logger.info("[GUI] Получено событие перезапуска; текущая служба завершается...")
                    if not _stop_webui_process_tree(process):
                        logger.error_context(
                            title="Не удалось остановить дочерний процесс WebUI",
                            reason="Команды terminate и kill отправлены, но прежний дочерний процесс WebUI всё ещё активен.",
                            impact="Запуск нового WebUI приведёт к конкуренции за порт прослушивания.",
                            action="Проверьте права управления процессами, завершите оставшийся процесс gui.py и перезапустите приложение.",
                            level=50,
                        )
                        should_exit = True
                        break
                    try:
                        force_dependency_sync = dependency_sync_event.is_set()
                    except OSError as exc:
                        logger.error_context(
                            title="Не удалось прочитать состояние синхронизации зависимостей",
                            exc=exc,
                            impact="Невозможно подтвердить синхронизацию обновлённого окружения; WebUI не будет перезапущена.",
                            action="Проверьте права чтения и записи каталога config, затем перезапустите приложение.",
                            level=50,
                        )
                        should_exit = True
                        break
                    if force_dependency_sync:
                        logger.info("[GUI] Обнаружен запрос обновления; зависимости будут синхронизированы до создания замещающего WebUI")
                    break
                elif not process.is_alive():
                    if time.monotonic() - ready_at >= WEBUI_STABLE_RUNTIME:
                        runtime_failures = 0
                    runtime_failures += 1
                    if runtime_failures >= WEBUI_RUNTIME_RETRY_LIMIT:
                        logger.error_context(
                            title="Служба WebUI AzurPilot неоднократно аварийно завершается",
                            reason=(
                                f"Служба завершилась {runtime_failures} раз до достижения стабильного времени работы без штатного события перезапуска."
                            ),
                            impact="WebUI больше не обслуживается; родительский процесс завершится во избежание бесконечного цикла сбоев.",
                            action="Изучите журнал GUI и ошибку дочернего процесса, затем перезапустите приложение.",
                            level=50,
                        )
                        should_exit = True
                    else:
                        logger.warning(
                            f"[GUI] WebUI неожиданно завершилась; повтор через {runtime_failures} с ({runtime_failures}/{WEBUI_RUNTIME_RETRY_LIMIT})"
                        )
                        time.sleep(runtime_failures)
                    break

            # 确保子进程完全退出；清理失败时不能创建替代 WebUI。
            if not _stop_webui_process_tree(process):
                if not should_exit:
                    logger.error_context(
                        title="Не удалось очистить дочерний процесс WebUI",
                        reason="Дочерний процесс завершился или требует перезапуска, но связанные worker не удалось подтвердить как остановленные.",
                        impact="Запуск нового WebUI может оставить дублирующиеся задачи управления устройствами.",
                        action="Проверьте оставшиеся процессы gui.py и worker, затем перезапустите приложение.",
                        level=50,
                    )
                should_exit = True
    finally:
        _stop_webui_process_tree(process)
        _stop_dependency_sync_service(service, service_request_queue)
        logger.info("[GUI] Служба WebUI AzurPilot успешно завершена")


def _run_webui_without_reload() -> bool:
    """Восстановить orphaned worker-процессы перед прямым запуском WebUI."""
    if not _recover_orphaned_workers():
        return False
    func(None, None)
    return True


if __name__ == "__main__":
    # 设置multiprocessing启动方式为spawn（macOS兼容性要求）
    try:
        set_start_method("spawn", force=True)
        # 额外的macOS环境配置
        if os.name == "posix" and sys.platform == "darwin":
            os.environ["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] = "YES"
    except RuntimeError:
        logger.warning("[GUI] Не удалось установить метод запуска spawn; возможно использование fork, что не рекомендуется в macOS")

    if State.deploy_config.EnableReload:
        run_webui_supervisor()
    else:
        _run_webui_without_reload()
