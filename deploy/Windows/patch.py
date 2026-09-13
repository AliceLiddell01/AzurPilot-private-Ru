import os
import re

from deploy.Windows.logger import logger


def patch_trust_env(file):
    """Отключить наследование глобальных proxy в исходнике requests."""
    try:
        with open(file, 'r', encoding='utf-8') as f:
            content = f.read()
    except FileNotFoundError:
        logger.info(f'{file}: trust_env не существует')
        return

    if re.search('self.trust_env = True', content):
        content = re.sub('self.trust_env = True', 'self.trust_env = False', content)
        with open(file, 'w', encoding='utf-8') as f:
            f.write(content)
        logger.info(f'{file}: trust_env исправлен')
    elif re.search('self.trust_env = False', content):
        logger.info(f'{file}: trust_env уже исправлен')
    else:
        logger.info(f'{file}: trust_env отсутствует в файле')


def check_running_directory():
    """Проверить, что установщик запущен не из временного архива."""
    file = __file__.replace(r"\\", "/").replace("\\", "/")
    if 'Temp/360zip' in file:
        logger.critical('Сначала распакуйте архив AzurPilot, затем установите AzurPilot')
        exit(1)
    if 'Temp/Rar' in file or 'Local/Temp' in file:
        logger.critical('Сначала распакуйте установщик AzurPilot')
        exit(1)


def pre_checks():
    check_running_directory()


if __name__ == '__main__':
    pre_checks()
