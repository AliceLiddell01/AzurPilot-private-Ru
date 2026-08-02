"""配置文件监控模块。

定义 ConfigWatcher 类，通过跟踪配置文件的修改时间来检测文件变更，
支持在任务间自动热重载配置，避免重启应用。
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
        """获取配置文件的最后修改时间。"""
        timestamp = os.stat(filepath_config(self.config_name)).st_mtime
        mtime = datetime.fromtimestamp(timestamp).replace(microsecond=0)
        return mtime

    def should_reload(self) -> bool:
        """检查配置文件是否已被修改，需要重新加载。

        Returns:
            bool: 文件是否已修改。
        """
        mtime = self.get_mtime()
        if mtime > self.start_mtime:
            logger.info(f'[Конфигурация: наблюдение] "{self.config_name}" изменена в {mtime}')
            return True
        else:
            return False
