"""游戏去和谐处理。

通过 ADB 推送 localization.txt 文件到模拟器，启用游戏内置的
本地化皮肤显示。该任务只处理本地文件和设备部署，不更新代码仓库。
"""

import shutil

from deploy.utils import *
from module.handler.login import LoginHandler
from module.logger import logger

localization_txt = """
Localization = true
Localization_skin = true
""".strip() + '\n'


class AzurLaneUncensored(LoginHandler):
    def create_level1_uncensored(self):
        logger.info('Создание файла локализации без цензуры первого уровня')
        folder = './files'
        try:
            shutil.rmtree(folder)
        except FileNotFoundError:
            pass
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, 'localization.txt'), 'w', encoding='utf-8') as f:
            f.write(localization_txt)

    def run(self):
        """
        This will do:
        1. Create the localization override
        2. Adb push to emulator
        3. Restart game
        """
        folder = './.venv/AzurLaneUncensored'

        logger.hr('Подготовка файлов Azur Lane без цензуры', level=1)
        os.makedirs(folder, exist_ok=True)
        previous_folder = os.getcwd()

        try:
            os.chdir(folder)
            self.create_level1_uncensored()

            logger.hr('Отправка файлов без цензуры', level=1)
            logger.info('[Daemon-Без цензуры] Отправка займёт несколько секунд')
            command = ['push', 'files', f'/sdcard/Android/data/{self.device.package}']
            logger.info(f'[Daemon-Без цензуры] Команда: {command}')
            self.device.adb_command(command, timeout=30)
            logger.info('[Daemon-Без цензуры] Файлы успешно отправлены')
        finally:
            os.chdir(previous_folder)

        logger.hr('Перезапуск Azur Lane', level=1)
        self.config.override(Error_HandleError=True)
        self.device.app_stop()
        self.device.app_start()
        self.handle_app_login()

        logger.info('[Daemon-Без цензуры] Готово')


if __name__ == '__main__':
    AzurLaneUncensored('alas', task='AzurLaneUncensored').run()
