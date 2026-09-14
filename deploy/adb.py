from deploy.config import DeployConfig
from deploy.emulator import EmulatorConnect
from deploy.logger import logger
from deploy.utils import *


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
