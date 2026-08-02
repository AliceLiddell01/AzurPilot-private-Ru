import filecmp
import os
import shutil

from deploy.Windows.config import DeployConfig
from deploy.Windows.logger import Progress, logger


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
                # "Update app.asar" 关键字用于 AlasApp 判断是否有热更新
                logger.info(f'Обновление app.asar [Update app.asar] {update} -----> {source}')
                os.remove(source)
                shutil.copy(update, source)
                return True
        else:
            logger.info(f'{source} не существует, обновление пропущено')
            return False

    def app_update(self):
        logger.hr('Обновление приложения', 0)

        if not self.AppAsarUpdate:
            logger.info('AppAsarUpdate отключён, обновление пропущено')
            Progress.UpdateAlasApp()
            return False
