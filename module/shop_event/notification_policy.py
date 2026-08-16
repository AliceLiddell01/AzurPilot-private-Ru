"""EventShop-scoped notification semantics for the shared scheduler."""

from __future__ import annotations

from typing import Any

from module.logger import logger

_DISABLED_ONEPUSH = "provider: null"


def apply_event_shop_notification_policy(config: Any) -> bool:
    """Make EventShop's scheduler toggle mean error-only push.

    The generic scheduler interprets ``Scheduler_PushNotification`` as a
    completion notification and can emit it after successful or recoverable
    task results.  EventShop intentionally uses the persisted
    ``EventShop.Scheduler.PushNotification`` preference only as permission to
    deliver error OnePush messages.

    Returns:
        bool: Whether EventShop error push is enabled by the user.
    """
    push_on_error = bool(
        config.cross_get(
            keys="EventShop.Scheduler.PushNotification",
            default=False,
        )
    )

    # Never let the shared scheduler turn this EventShop preference into a
    # success/recoverable completion push.
    overrides = {"Scheduler_PushNotification": False}

    # Existing exception paths already use Error_OnePushConfig.  Disable that
    # transport only for this bound EventShop config when the user has not
    # requested error pushes.  The scheduler reloads config between tasks, so
    # this does not mutate another task's persisted notification settings.
    if not push_on_error:
        overrides["Error_OnePushConfig"] = _DISABLED_ONEPUSH

    config.override(**overrides)
    logger.info(
        "[Магазин события] Push-уведомления: только ошибки, "
        + ("включены" if push_on_error else "выключены")
    )
    return push_on_error
