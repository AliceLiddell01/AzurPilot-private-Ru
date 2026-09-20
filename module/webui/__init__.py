"""Web界面模块。"""

# Должен импортироваться первым, чтобы инициализировать каталог логов.
from module.logger import logger
import deploy.logger

deploy.logger.logger = logger
