"""Оркестрация безопасного восстановления эмулятора для планировщика.

Планировщик решает, когда разрешена эскалация. Этот модуль выполняет одну
ограниченную цепочку: штатная остановка → при разрешении instance-scoped hard
kill → холодный запуск и проверка загрузки → создание нового Device. Финальная
проверка Azur Lane остаётся в Stage 1 helper планировщика и не запускается
рекурсивно.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from module.logger import logger


@dataclass
class EmulatorRecoveryOutcome:
    success: bool
    stage: str
    mode: str = ''
    instance_name: str = ''
    device: Any = None


def _fresh_recovery_device(config):
    """Создать Device без скрытого повторного запуска эмулятора из конструктора."""
    from module.device.device import Device

    class FreshRecoveryDevice(Device):
        def emulator_start(self):
            logger.warning(
                '[Восстановление] Внутренний autostart Device подавлен: '
                'destructive retry уже исчерпан внешней Stage 2 цепочкой'
            )
            return False

    return FreshRecoveryDevice(config=config)


def _release_current_device(current_device) -> None:
    """Освободить screenshot/IPC ресурсы старого Device перед остановкой эмулятора."""
    if current_device is None:
        return

    release = getattr(current_device, 'release_during_wait', None)
    if not callable(release):
        return

    try:
        release()
        logger.info('[Восстановление] Ресурсы старого Device освобождены')
    except Exception as exc:
        logger.warning(
            f'[Восстановление] Не удалось полностью освободить ресурсы старого Device: {exc}'
        )


def recover_emulator_transport(
        config,
        *,
        current_device=None,
        allow_hard_kill: bool,
        platform=None,
        device_factory: Callable | None = None,
) -> EmulatorRecoveryOutcome:
    """Выполнить ровно одну транспортную цепочку восстановления эмулятора."""
    if platform is None:
        # Stage 2 намеренно не переиспользует обычный current_device.platform:
        # на Windows recovery lifecycle должен быть изолирован от глобального
        # PlatformWindows и его исторических startup/cleanup путей.
        from module.device.platform import get_recovery_platform
        platform = get_recovery_platform(config)

    instance = getattr(platform, 'emulator_instance', None)
    if instance is None:
        logger.error('[Восстановление] Сбой на этапе: определение целевого эмулятора')
        return EmulatorRecoveryOutcome(False, 'target-resolution')

    instance_name = getattr(instance, 'name', '') or str(instance)
    logger.info(f'[Восстановление] Целевой экземпляр эмулятора определён: {instance_name}')
    _release_current_device(current_device)
    logger.info('[Восстановление] Запрошена штатная остановка эмулятора')

    try:
        graceful_stopped = bool(platform.emulator_stop())
    except Exception as exc:
        logger.exception_context(
            title='Ошибка штатной остановки эмулятора',
            exc=exc,
            impact='Фактическая остановка не подтверждена; восстановление не может продолжиться без проверки состояния.',
            action='Проверьте manager API и принадлежность процессов выбранному экземпляру.',
        )
        graceful_stopped = False

    mode = 'graceful'
    if graceful_stopped:
        logger.info('[Восстановление] Штатная остановка подтверждена')
    else:
        if not allow_hard_kill:
            logger.error('[Восстановление] Сбой на этапе: проверка штатной остановки')
            return EmulatorRecoveryOutcome(
                False,
                'graceful-stop',
                mode=mode,
                instance_name=instance_name,
            )

        force_stop = getattr(platform, 'emulator_force_stop_instance', None)
        if not callable(force_stop):
            logger.error('[Восстановление] Сбой на этапе: instance-scoped hard kill недоступен')
            return EmulatorRecoveryOutcome(
                False,
                'hard-kill-unavailable',
                mode=mode,
                instance_name=instance_name,
            )

        logger.warning('[Восстановление] Целевой экземпляр всё ещё запущен; начинается instance-scoped hard kill')
        try:
            hard_stopped = bool(force_stop())
        except Exception as exc:
            logger.exception_context(
                title='Ошибка instance-scoped hard kill эмулятора',
                exc=exc,
                impact='Старый экземпляр может оставаться запущенным; холодный запуск запрещён.',
                action='Проверьте права процесса и identity целевого экземпляра.',
            )
            hard_stopped = False

        if not hard_stopped:
            logger.error('[Восстановление] Сбой на этапе: проверка hard kill')
            return EmulatorRecoveryOutcome(
                False,
                'hard-kill',
                mode=mode,
                instance_name=instance_name,
            )
        mode = 'hard-kill'
        logger.info('[Восстановление] Остановка целевого экземпляра после hard kill подтверждена')

    logger.info('[Восстановление] Холодный запуск эмулятора')
    try:
        started = bool(platform.emulator_start())
    except Exception as exc:
        logger.exception_context(
            title='Ошибка холодного запуска эмулятора',
            exc=exc,
            impact='Восстановление не завершено; новый Device создавать нельзя.',
            action='Проверьте launch_player и проверку загрузки выбранного экземпляра.',
        )
        started = False

    if not started:
        logger.error('[Восстановление] Сбой на этапе: холодный запуск или проверка загрузки')
        return EmulatorRecoveryOutcome(
            False,
            'cold-start',
            mode=mode,
            instance_name=instance_name,
        )

    logger.info('[Восстановление] Проверка загрузки эмулятора пройдена')

    if device_factory is None:
        device_factory = _fresh_recovery_device

    try:
        fresh_device = device_factory(config=config)
    except Exception as exc:
        logger.exception_context(
            title='Не удалось создать новый Device после перезапуска эмулятора',
            exc=exc,
            impact='Старые ADB/screenshot/control handles не переиспользуются; восстановление считается неуспешным.',
            action='Проверьте ADB, выбранный serial и методы получения снимков и управления после загрузки эмулятора.',
        )
        logger.error('[Восстановление] Сбой на этапе: создание нового Device')
        return EmulatorRecoveryOutcome(
            False,
            'fresh-device',
            mode=mode,
            instance_name=instance_name,
        )

    logger.info('[Восстановление] Создан новый Device')
    return EmulatorRecoveryOutcome(
        True,
        'transport-ready',
        mode=mode,
        instance_name=instance_name,
        device=fresh_device,
    )
