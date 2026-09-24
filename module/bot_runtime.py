"""Canonical headless composition root for Bot Runtime."""

from __future__ import annotations

import os
from pathlib import Path

from module.application.bot_runtime_owner import BotRuntimeOwner
from module.logger import configure_runtime_logging, logger


def _build_notification_runtime() -> object | None:
    identity_keys = (
        "AZURPILOT_NOTIFICATION_AGENT_ID",
        "AZURPILOT_NOTIFICATION_AGENT_PROFILES",
        "AZURPILOT_NOTIFICATION_AGENT_TOKEN",
        "AZURPILOT_NOTIFICATION_AGENT_TOKEN_FILE",
    )
    if not any(key in os.environ for key in identity_keys):
        return None
    from module.persistence.runtime import (
        bootstrap_runtime_storage,
        build_runtime_notification_composition,
        build_runtime_notification_telemetry,
    )

    bootstrap_runtime_storage(require_ready=True)
    telemetry = build_runtime_notification_telemetry()
    return build_runtime_notification_composition(telemetry=telemetry)


def main() -> int:
    repository_root = Path(__file__).resolve().parents[1]
    os.environ["AZURPILOT_REPOSITORY_ROOT"] = str(repository_root)
    configure_runtime_logging(name="bot-runtime", observability_component="bot_runtime")
    notification_runtime = None
    owner: BotRuntimeOwner | None = None
    server = None
    try:
        notification_runtime = _build_notification_runtime()
        if notification_runtime is not None:
            start = getattr(notification_runtime, "start", None)
            if callable(start):
                start()
        owner = BotRuntimeOwner(
            repository_root,
            notification_service=notification_runtime,
        )
        server = owner.start_server()
        owner.start_configured_profiles()
        logger.info("[Bot Runtime] Headless owner запущен")
        owner.wait_for_shutdown()
        return 0
    except KeyboardInterrupt:
        logger.info("[Bot Runtime] Получен запрос завершения процесса")
        return 0
    except Exception as exc:  # noqa: BLE001 - entrypoint сообщает причину и завершает bootstrap.
        logger.exception("[Bot Runtime] Не удалось запустить owner: %s", exc)
        return 1
    finally:
        if server is not None:
            server.close()
        if owner is not None:
            try:
                owner.close()
            except Exception as exc:  # noqa: BLE001 - ownership нельзя снять при оставшихся worker.
                logger.error("[Bot Runtime] Owner не освобождён: %s", type(exc).__name__)
        if notification_runtime is not None:
            try:
                stop = getattr(notification_runtime, "stop", None)
                if callable(stop):
                    stop()
            except Exception as exc:  # noqa: BLE001 - завершение worker продолжается независимо от dispatcher.
                logger.error("[Bot Runtime] Notification service не остановлен: %s", type(exc).__name__)


if __name__ == "__main__":
    raise SystemExit(main())
