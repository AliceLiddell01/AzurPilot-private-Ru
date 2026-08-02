import filecmp
import shutil

from deploy.config import DeployConfig
from deploy.logger import logger
from deploy.utils import *


class AppManager(DeployConfig):
    @staticmethod
    def app_asar_replace(folder, path='./.venv/WebApp/resources/app.asar'):
        """替换 app.asar 文件以更新 WebApp。

        Args:
            folder (str): AzurPilot 根目录路径。
            path (str): 从根目录到 app.asar 的相对路径。

        Returns:
            bool: 是否已更新。
        """
        source = os.path.abspath(os.path.join(folder, path))
        logger.info(f'Текущий файл: {source}')

        try:
            import alas_webapp
        except ImportError:
            logger.info('Зависимость alas_webapp отсутствует, обновление пропущено')
            return False

        update = alas_webapp.app_file()
        logger.info(f'Новая версия: {alas_webapp.__version__}')
        logger.info(f'Новый файл: {update}')

        if os.path.exists(source):
            if filecmp.cmp(source, update, shallow=True):
                logger.info('app.asar уже обновлён')
                return False
            else:
                logger.info(f'Копирование {update} -----> {source}')
                os.remove(source)
                shutil.copy(update, source)
                return True
        else:
            logger.info(f'{source} не существует, обновление пропущено')
            return False

    def app_update(self):
        logger.hr('Обновление app.asar', 0)

        return self.app_asar_replace(os.getcwd())
