"""Instance-scoped identity процессов и hard-kill primitives современного MuMu.

Модуль не зависит от winreg/WinAPI и поэтому тестируется на любой платформе.
Целевой набор строится только от exact MuMuNxDevice.exe выбранного экземпляра
и его дочерних процессов. Общие процессы MuMu сюда не попадают.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Protocol

import psutil

from module.logger import logger


class MuMuInstanceLike(Protocol):
    name: str

    @property
    def MuMuPlayer12_id(self) -> int | None:
        ...


class MuMuInstanceIdentityError(RuntimeError):
    """Набор процессов выбранного экземпляра нельзя определить однозначно."""


def _process_name(proc: psutil.Process) -> str:
    try:
        return proc.name() or ''
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
        return ''


def _process_cmdline(proc: psutil.Process) -> list[str]:
    try:
        return list(proc.cmdline())
    except psutil.AccessDenied as exc:
        raise MuMuInstanceIdentityError(
            f'Нет доступа к command line MuMuNxDevice PID {proc.pid}; identity нельзя доказать'
        ) from exc
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return []


def _argument_value(tokens: list[str], flag: str) -> str | None:
    for index, token in enumerate(tokens[:-1]):
        if token.casefold() == flag.casefold():
            return tokens[index + 1]
    return None


def is_mumu_instance_root(proc: psutil.Process, instance: MuMuInstanceLike) -> bool:
    """Проверить строгую identity корневого MuMuNxDevice процесса."""
    instance_id = instance.MuMuPlayer12_id
    if instance_id is None or not instance.name:
        return False
    if _process_name(proc).casefold() != 'mumunxdevice.exe':
        return False

    tokens = _process_cmdline(proc)
    if not tokens:
        return False

    return (
        _argument_value(tokens, '-v') == str(instance_id)
        and _argument_value(tokens, '--vm') == instance.name
    )


def find_mumu_instance_roots(
        instance: MuMuInstanceLike,
        processes: Iterable[psutil.Process] | None = None,
) -> list[psutil.Process]:
    """Найти корневой MuMuNxDevice только выбранного экземпляра."""
    if instance.MuMuPlayer12_id is None or not instance.name:
        raise MuMuInstanceIdentityError(
            f'Неполная identity MuMu instance: name={instance.name!r}, id={instance.MuMuPlayer12_id!r}'
        )

    if processes is None:
        processes = psutil.process_iter()

    roots = [proc for proc in processes if is_mumu_instance_root(proc, instance)]
    if len(roots) > 1:
        pids = ', '.join(str(proc.pid) for proc in roots)
        raise MuMuInstanceIdentityError(
            f'Найдено несколько MuMuNxDevice для {instance.name}: PID {pids}'
        )
    return roots


def is_mumu_instance_running(instance: MuMuInstanceLike) -> bool:
    """Проверить фактическое состояние instance без зависимости от ADB или окна."""
    return bool(find_mumu_instance_roots(instance))


def wait_mumu_instance_stopped(
        instance: MuMuInstanceLike,
        *,
        timeout: float = 15.0,
        interval: float = 0.5,
) -> bool:
    """Ограниченно ждать исчезновения корневого instance-owned процесса."""
    deadline = time.monotonic() + timeout
    while True:
        if not is_mumu_instance_running(instance):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def mumu_instance_owned_processes(instance: MuMuInstanceLike) -> list[psutil.Process]:
    """Получить ограниченный target set: exact root и его descendants."""
    roots = find_mumu_instance_roots(instance)
    if not roots:
        return []

    root = roots[0]
    try:
        children = root.children(recursive=True)
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess) as exc:
        raise MuMuInstanceIdentityError(
            f'Не удалось безопасно перечислить дочерние процессы PID {root.pid}: {exc}'
        ) from exc

    # Корень всегда хранится первым: force-stop не зависит от неявного порядка
    # обхода descendants и гарантированно завершает root последним.
    unique = {root.pid: root}
    for proc in children:
        unique.setdefault(proc.pid, proc)
    return list(unique.values())


def force_stop_mumu_instance(
        instance: MuMuInstanceLike,
        *,
        timeout: float = 10.0,
) -> bool:
    """Завершить только доказанно instance-owned process tree и проверить остановку."""
    try:
        targets = mumu_instance_owned_processes(instance)
    except MuMuInstanceIdentityError as exc:
        logger.error(f'[Устройство — Windows] Hard kill MuMu отклонён: {exc}')
        return False

    if not targets:
        logger.info(f'[Устройство — Windows] MuMu instance {instance.name} уже остановлен')
        return True

    root = targets[0]
    ordered = [proc for proc in targets[1:] if proc.pid != root.pid] + [root]
    signaled_pids: list[int] = []
    for proc in ordered:
        try:
            logger.warning(
                f'[Устройство — Windows] Instance-scoped hard kill: {instance.name}, '
                f'PID={proc.pid}, process={_process_name(proc) or "<unknown>"}'
            )
            proc.kill()
            signaled_pids.append(proc.pid)
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as exc:
            remaining = [item.pid for item in ordered if item.pid not in signaled_pids]
            logger.error(
                f'[Устройство — Windows] Нет прав для hard kill PID={proc.pid} '
                f'instance {instance.name}: {exc}; уже отправлены сигналы PID={signaled_pids or "нет"}; '
                f'необработанные PID={remaining or "нет"}'
            )
            return False
        except psutil.Error as exc:
            remaining = [item.pid for item in ordered if item.pid not in signaled_pids]
            logger.error(
                f'[Устройство — Windows] Ошибка hard kill PID={proc.pid} '
                f'instance {instance.name}: {exc}; уже отправлены сигналы PID={signaled_pids or "нет"}; '
                f'необработанные PID={remaining or "нет"}'
            )
            return False

    _, alive = psutil.wait_procs(targets, timeout=timeout)
    if alive:
        pids = ', '.join(str(proc.pid) for proc in alive)
        logger.error(
            f'[Устройство — Windows] После hard kill остаются процессы instance '
            f'{instance.name}: PID {pids}'
        )
        return False

    try:
        stopped = not is_mumu_instance_running(instance)
    except MuMuInstanceIdentityError as exc:
        logger.error(f'[Устройство — Windows] Не удалось проверить hard kill: {exc}')
        return False

    if stopped:
        logger.info(f'[Устройство — Windows] Hard kill подтверждён для {instance.name}')
    else:
        logger.error(f'[Устройство — Windows] MuMu instance {instance.name} остаётся запущен после hard kill')
    return stopped
