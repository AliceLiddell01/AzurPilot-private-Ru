"""Device package bootstrap for Stage 8B runtime message compatibility."""

from module.logger import logger

_STAGE8B_DEVICE_MESSAGES = {
    "[设备-基准测试] 运行OCR设备基准测试": "[Устройство — OCR benchmark] Запуск выбора устройства OCR",
}

if not getattr(logger, "_stage8b_device_message_filter", False):
    _original_info = logger.info

    def _stage8b_info(message, *args, **kwargs):
        if isinstance(message, str):
            message = _STAGE8B_DEVICE_MESSAGES.get(message, message)
        return _original_info(message, *args, **kwargs)

    logger.info = _stage8b_info
    logger._stage8b_device_message_filter = True
