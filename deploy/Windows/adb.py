from deploy.Windows.emulator import EmulatorManager
from deploy.Windows.logger import Progress, logger


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
