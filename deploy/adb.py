import logging

from deploy.config import DeployConfig
from deploy.emulator import EmulatorConnect
from deploy.logger import logger
from deploy.utils import *

IGNORE_SERIAL = [
    # Дисплей жидкостного охлаждения, см. https://github.com/LmeSzinc/AzurLaneAutoScript/issues/3412
    'HRBDFUN',
    # USB-сетевой адаптер
    '1234567890ABCDEF',
]


def show_fix_tip(module):
    from deploy.uv import venv_uv

    uv = venv_uv()
    logger.info(f"""
    Чтобы исправить ошибку:
    1. Повторно запустите программу запуска, чтобы uv обновил локальную .venv
    2. Если проблема сохраняется, выполните:
        "{uv}" sync --frozen --no-dev --no-install-project --reinstall-package {module}
    3. Снова откройте AzurPilot
    """)


class AdbManager(DeployConfig):
    @cached_property
    def adb(self):
        exe = self.filepath('AdbExecutable')
        if os.path.exists(exe):
            return exe

        logger.warning(f'AdbExecutable: {exe} не существует, вместо него используется `adb`')
        return 'adb'

    def adb_install(self):
        logger.hr('Запуск службы ADB', 0)

        emulator = EmulatorConnect(adb=self.adb)
        if self.ReplaceAdb:
            logger.hr('Замена ADB', 1)
            emulator.adb_replace()
        elif self.AutoConnect:
            logger.hr('Подключение ADB', 1)
            emulator.brute_force_connect()

        if False:
            logger.hr('Инициализация uiautomator2', 1)
            try:
                import adbutils
                from uiautomator2 import init
            except ModuleNotFoundError as e:
                message = str(e)
                for module in ['apkutils2', 'progress']:
                    # Типичная ошибка отсутствующего модуля
                    if module in message:
                        show_fix_tip(module)
                        exit(1)
                raise

            # Удаляем глобальные настройки прокси, иначе uiautomator2 будет использовать прокси
            for k in list(os.environ.keys()):
                if k.lower().endswith('_proxy'):
                    del os.environ[k]

            for device in adbutils.adb.iter_device():
                if device.serial in IGNORE_SERIAL:
                    continue
                logger.info(f'Инициализация устройства {device}')
                initer = init.Initer(device, loglevel=logging.DEBUG)
                # В MuMu X нет ro.product.cpu.abi, поэтому берём первое значение из ro.product.cpu.abilist
                if initer.abi not in ['x86_64', 'x86', 'arm64-v8a', 'armeabi-v7a', 'armeabi']:
                    initer.abi = initer.abis[0]
                # Пропускаем устройство, если команда getprop недоступна
                if 'getprop' in initer.abi:
                    logger.warning(f'Не удалось выполнить getprop на устройстве {device}, результат: {initer.abi}')
                    continue
                initer.set_atx_agent_addr('127.0.0.1:7912')

                try:
                    initer.install()
                except AssertionError:
                    logger.info(f'AssertionError при установке uiautomator2 на устройство {device.serial}')
                    logger.info('Если вы используете BlueStacks, LDPlayer или WSA, '
                                'включите ADB в настройках эмулятора')
                    exit(1)
                except ConnectionError:
                    logger.error('Не удалось установить ресурсы uiautomator2; внешний fallback отключён')
                    raise

                initer._device.shell(["rm", "/data/local/tmp/minicap"])
                initer._device.shell(["rm", "/data/local/tmp/minicap.so"])
