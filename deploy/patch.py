import os
import re

from deploy.logger import logger
from deploy.uv import venv_python


def patch_trust_env(file):
    """修补 requests 库的 trust_env 设置。

    用户的代理软件即使未运行也会留下全局代理设置。
    虽然在代码中设置了 `session.trust_env = False`，但这不影响 pip 命令。
    因此直接修补 requests 源码，将 trust_env 强制设为 False。

    Returns:
        bool: 是否已修补。
    """
    if os.path.exists(file):
        with open(file, 'r', encoding='utf-8') as f:
            content = f.read()
        if re.search('self.trust_env = True', content):
            content = re.sub('self.trust_env = True', 'self.trust_env = False', content)
            with open(file, 'w', encoding='utf-8') as f:
                f.write(content)
            logger.info(f'{file}: trust_env исправлен')
        elif re.search('self.trust_env = False', content):
            logger.info(f'{file}: trust_env уже исправлен')
        else:
            logger.info(f'{file}: trust_env не найден')
    else:
        logger.info(f'{file}: исправление trust_env не требуется')


def check_running_directory():
    """防呆检查：检测是否在压缩软件的临时目录中运行。

    如果用户直接在压缩软件中运行安装器，会因临时目录导致安装失败。
    """
    file = __file__.replace(r"\\", "/").replace("\\", "/")
    # C:/Users/<user>/AppData/Local/Temp/360zip$temp/360$3/AzurLaneAutoScript
    if 'Temp/360zip' in file:
        logger.critical('Сначала распакуйте архив AzurPilot, затем установите AzurPilot')
        exit(1)
    # C:/Users/<user>/AppData/Local/Temp/Rar$EXa9248.23428/AzurLaneAutoScript
    if 'Temp/Rar' in file or 'Local/Temp' in file:
        logger.critical('Сначала распакуйте установщик AzurPilot')
        exit(1)


def patch_uiautomator2():
    """Оставить совместимую точку входа для uiautomator2 3.x."""
    logger.info('uiautomator2 3.x использует встроенные ресурсы; legacy patch не требуется')


def pre_checks():
    check_running_directory()

    patch_uiautomator2()


if __name__ == '__main__':
    pre_checks()
