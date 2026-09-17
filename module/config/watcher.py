"""Модуль наблюдения за файлами конфигурации.

Определяет класс ConfigWatcher, отслеживающий изменения файлов конфигурации
по времени их последней модификации. Обеспечивает автоматическую горячую
перезагрузку конфигурации между задачами без перезапуска приложения.
"""

import os
from datetime import datetime

from module.config.utils import DEFAULT_CONFIG_NAME, filepath_config, DEFAULT_TIME
from module.logger import logger


class ConfigWatcher:
    config_name = DEFAULT_CONFIG_NAME
    start_mtime = DEFAULT_TIME

    def start_watching(self) -> None:
        self.start_mtime = self.get_mtime()

    def get_mtime(self) -> datetime:
        """Получить время последней модификации файла конфигурации."""
        timestamp = os.stat(filepath_config(self.config_name)).st_mtime
        mtime = datetime.fromtimestamp(timestamp).replace(microsecond=0)
        return mtime

    def should_reload(self) -> bool:
        """Проверить, был ли файл конфигурации изменён и требуется ли перезагрузка.

        Returns:
            bool: Был ли файл модифицирован.
        """
        mtime = self.get_mtime()
        if mtime > self.start_mtime:
            logger.info(f'[Конфигурация: наблюдение] "{self.config_name}" изменена в {mtime}')
            return True
        else:
            return False
