import logging
import os

from deploy.Windows.emulator import EmulatorManager
from deploy.Windows.logger import Progress, logger


def show_fix_tip(module):
    logger.info(f"""
    Чтобы исправить ошибку:
    1. Повторно запустите программу запуска, чтобы uv обновил локальную .venv
    2. Если проблема сохраняется, выполните:
        ./.venv/Scripts/uv.exe sync --frozen --no-dev --no-install-project --reinstall-package {module}
    3. Снова откройте AzurPilot.exe
    """)


class AdbManager(EmulatorManager):
    def adb_install(self):
        logger.hr('Запуск службы ADB', 0)

        if self.ReplaceAdb:
            logger.hr('Замена ADB', 1)
            self.adb_replace()
            Progress.AdbReplace()
        if self.AutoConnect:
            logger.hr('Подключение ADB', 1)
            self.brute_force_connect()
            Progress.AdbConnect()

        if False:
            logger.hr('Инициализация uiautomator2', 1)
            try:
                import adbutils
                from uiautomator2 import init
            except ModuleNotFoundError as e:
                message = str(e)
                for module in ['apkutils2', 'progress']:
                    # Распространённая ошибка отсутствующего модуля.
                    if module in message:
                        show_fix_tip(module)
                        exit(1)
                raise

            # Удаляем глобальные настройки прокси, иначе uiautomator2 будет использовать прокси.
            for k in list(os.environ.keys()):
                if k.lower().endswith('_proxy'):
                    del os.environ[k]

            for device in adbutils.adb.iter_device():
                initer = init.Initer(device, loglevel=logging.DEBUG)
                # У MuMu X отсутствует ro.product.cpu.abi, поэтому берём первый элемент из ro.product.cpu.abilist.
                if initer.abi not in ['x86_64', 'x86', 'arm64-v8a', 'armeabi-v7a', 'armeabi']:
                    initer.abi = initer.abis[0]
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
