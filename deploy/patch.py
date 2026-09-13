import os
import re

from deploy.logger import logger


def patch_trust_env(file):
    """Исправить настройку trust_env в библиотеке requests.

    Даже неработающий пользовательский proxy может оставить глобальные
    настройки. ``session.trust_env = False`` не влияет на команду pip, поэтому
    здесь при необходимости исправляется исходник requests.

    Returns:
        bool: признак выполненного исправления.
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
    """Проверить, что установщик запускается не из временного архива.

    Запуск прямо из окна архиватора приводит к ошибке установки.
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


def pre_checks():
    check_running_directory()


if __name__ == '__main__':
    pre_checks()
