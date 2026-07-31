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
        logger.info('创建1级未审查')
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

        logger.hr('准备 AzurLane 未审查文件', level=1)
        os.makedirs(folder, exist_ok=True)
        previous_folder = os.getcwd()

        try:
            os.chdir(folder)
            self.create_level1_uncensored()

            logger.hr('推送未审查文件', level=1)
            logger.info('[守护-无删减] 推送需要几秒钟')
            command = ['push', 'files', f'/sdcard/Android/data/{self.device.package}']
            logger.info(f'[守护-无删减] 命令: {command}')
            self.device.adb_command(command, timeout=30)
            logger.info('[守护-无删减] 推送成功')
        finally:
            os.chdir(previous_folder)

        logger.hr('重启碧蓝航线', level=1)
        self.config.override(Error_HandleError=True)
        self.device.app_stop()
        self.device.app_start()
        self.handle_app_login()

        logger.info('[守护-无删减] 完成')


if __name__ == '__main__':
    AzurLaneUncensored('alas', task='AzurLaneUncensored').run()
