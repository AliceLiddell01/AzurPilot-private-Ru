"""Политика уведомлений EventShop поверх общего планировщика."""

from __future__ import annotations

from typing import Any

from module.logger import logger

_DISABLED_ONEPUSH = "provider: null"


def apply_event_shop_notification_policy(config: Any) -> bool:
    """Использовать переключатель EventShop только для уведомлений об ошибках.

    Общий планировщик трактует ``Scheduler_PushNotification`` как разрешение
    уведомлять о завершении задачи, в том числе после успешного или
    восстанавливаемого результата. Для EventShop сохранённая настройка
    ``EventShop.Scheduler.PushNotification`` означает только разрешение
    отправлять сообщения OnePush об ошибках.

    Возвращает:
        bool: разрешены ли пользователем уведомления EventShop об ошибках.
    """
    push_on_error = bool(
        config.cross_get(
            keys="EventShop.Scheduler.PushNotification",
            default=False,
        )
    )

    # Не позволяем общему планировщику превратить настройку EventShop в
    # уведомление об успешном или восстанавливаемом завершении задачи.
    overrides = {"Scheduler_PushNotification": False}

    # Существующие пути обработки исключений уже используют Error_OnePushConfig.
    # Отключаем этот транспорт только для текущей конфигурации EventShop, если
    # пользователь не разрешил уведомления об ошибках. Между задачами
    # планировщик заново загружает конфигурацию, поэтому сохранённые настройки
    # другой задачи не изменяются.
    if not push_on_error:
        overrides["Error_OnePushConfig"] = _DISABLED_ONEPUSH

    config.override(**overrides)
    logger.info(
        "[Магазин события] Push-уведомления: только ошибки, "
        + ("включены" if push_on_error else "выключены")
    )
    return push_on_error
