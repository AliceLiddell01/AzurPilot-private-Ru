"""Оркестрация безопасного emulator recovery для scheduler.

Scheduler решает, когда разрешена эскалация. Этот модуль выполняет одну
bounded recovery-chain: graceful stop → при разрешении instance-scoped hard
kill → cold start/boot health → создание fresh Device. Финальная проверка
Azur Lane остаётся в Stage 1 helper scheduler и не запускается рекурсивно.
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


def recover_emulator_transport(
        config,
        *,
        current_device=None,
        allow_hard_kill: bool,
        platform=None,
        device_factory: Callable | None = None,
) -> EmulatorRecoveryOutcome:
    """Выполнить ровно одну transport-level recovery-chain эмулятора."""
    if platform is None:
        if current_device is not None and hasattr(current_device, 'platform'):
            platform = current_device.platform
        else:
            from module.device.platform import Platform
            platform = Platform(config, connect=False)

    instance = getattr(platform, 'emulator_instance', None)
    if instance is None:
        logger.error('[Recovery] recovery failed at: target emulator resolution')
        return EmulatorRecoveryOutcome(False, 'target-resolution')

    instance_name = getattr(instance, 'name', '') or str(instance)
    logger.info(f'[Recovery] target emulator resolved: {instance_name}')
    logger.info('[Recovery] graceful shutdown requested')

    try:
        graceful_stopped = bool(platform.emulator_stop())
    except Exception as exc:
        logger.exception_context(
            title='Ошибка graceful shutdown эмулятора',
            exc=exc,
            impact='Фактическая остановка не подтверждена; recovery не может продолжиться без проверки состояния.',
            action='Проверьте manager API и process ownership выбранного instance.',
        )
        graceful_stopped = False

    mode = 'graceful'
    if graceful_stopped:
        logger.info('[Recovery] graceful shutdown verified')
    else:
        if not allow_hard_kill:
            logger.error('[Recovery] recovery failed at: graceful shutdown verification')
            return EmulatorRecoveryOutcome(
                False,
                'graceful-stop',
                mode=mode,
                instance_name=instance_name,
            )

        force_stop = getattr(platform, 'emulator_force_stop_instance', None)
        if not callable(force_stop):
            logger.error('[Recovery] recovery failed at: instance-scoped hard kill unavailable')
            return EmulatorRecoveryOutcome(
                False,
                'hard-kill-unavailable',
                mode=mode,
                instance_name=instance_name,
            )

        logger.warning('[Recovery] target still alive; instance-scoped hard kill started')
        try:
            hard_stopped = bool(force_stop())
        except Exception as exc:
            logger.exception_context(
                title='Ошибка instance-scoped hard kill эмулятора',
                exc=exc,
                impact='Старый instance может оставаться жив; cold start запрещён.',
                action='Проверьте права процесса и identity target instance.',
            )
            hard_stopped = False

        if not hard_stopped:
            logger.error('[Recovery] recovery failed at: hard kill verification')
            return EmulatorRecoveryOutcome(
                False,
                'hard-kill',
                mode='hard-kill',
                instance_name=instance_name,
            )
        mode = 'hard-kill'
        logger.info('[Recovery] target shutdown verified after hard kill')

    logger.info('[Recovery] cold start')
    try:
        started = bool(platform.emulator_start())
    except Exception as exc:
        logger.exception_context(
            title='Ошибка cold start эмулятора',
            exc=exc,
            impact='Recovery не завершён; fresh Device создавать нельзя.',
            action='Проверьте launch_player и boot health выбранного instance.',
        )
        started = False

    if not started:
        logger.error('[Recovery] recovery failed at: cold start / boot health')
        return EmulatorRecoveryOutcome(
            False,
            'cold-start',
            mode=mode,
            instance_name=instance_name,
        )

    logger.info('[Recovery] boot health passed')

    if device_factory is None:
        from module.device.device import Device
        device_factory = Device

    try:
        fresh_device = device_factory(config=config)
    except Exception as exc:
        logger.exception_context(
            title='Не удалось создать fresh Device после перезапуска эмулятора',
            exc=exc,
            impact='Старые ADB/screenshot/control handles не переиспользуются; recovery считается неуспешным.',
            action='Проверьте ADB, выбранный serial и методы screenshot/control после загрузки эмулятора.',
        )
        logger.error('[Recovery] recovery failed at: fresh Device')
        return EmulatorRecoveryOutcome(
            False,
            'fresh-device',
            mode=mode,
            instance_name=instance_name,
        )

    logger.info('[Recovery] fresh Device created')
    return EmulatorRecoveryOutcome(
        True,
        'transport-ready',
        mode=mode,
        instance_name=instance_name,
        device=fresh_device,
    )
