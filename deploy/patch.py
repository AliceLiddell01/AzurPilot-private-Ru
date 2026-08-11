import os
import re
import subprocess

from deploy.logger import logger
from deploy.uv import venv_python


def site_packages_path():
    python = venv_python()
    if not python.exists():
        return None
    try:
        output = subprocess.check_output(
            [
                str(python),
                "-c",
                "import site; print(next(p for p in site.getsitepackages() if p.endswith('site-packages')))",
            ],
            text=True,
        ).strip()
    except Exception as exc:
        logger.info(f'Не удалось определить каталог site-packages в .venv: {exc}')
        return None
    return output.replace("\\", "/")


def site_package_file(*parts):
    root = site_packages_path()
    if not root:
        return None
    return os.path.join(root, *parts).replace("\\", "/")


def patch_trust_env(file):
    """修补 requests 库的 trust_env 设置。

    用户的代理软件即使未运行也会留下全局代理设置。
    虽然在代码中设置了 `session.trust_env = False`，但这不影响 pip 命令。
    因此直接修补 requests 源码，将 trust_env 强制设为 False。

    Returns:
        bool: 是否已修补。
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
    """防呆检查：检测是否在压缩软件的临时目录中运行。

    如果用户直接在压缩软件中运行安装器，会因临时目录导致安装失败。
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


def patch_uiautomator2():
    """修补 uiautomator2 的资源下载路径。

    uiautomator2 旧版安装器 может загружать ресурсы из внешних источников.
    AzurPilot использует локальный uiautomator2cache/cache, чтобы установка не зависела
    от скрытых сетевых fallback-адресов.

    Одновременно отключается установка minicap, который эмуляторам не требуется.
    """
    cache_dir = site_package_file('uiautomator2cache', 'cache')
    init_file = site_package_file('uiautomator2', 'init.py')
    appdir = "os.path.abspath(os.path.join(__file__, '../../uiautomator2cache'))"

    if not init_file or not os.path.exists(init_file):
        logger.info('uiautomator2 не установлен, исправление пропущено')
        return

    modified = False
    with open(init_file, 'r', encoding='utf-8') as f:
        content = f.read()

    # 修补 minicap_urls
    res = re.search(r'self.minicap_urls', content)
    if res:
        content = re.sub(r'self.minicap_urls', '[]', content)
        modified = True
        logger.info(f'{init_file}: minicap_urls исправлен')
    else:
        logger.info(f'{init_file}: исправление minicap_urls не требуется')

    # 修补 appdir
    if cache_dir and os.path.exists(cache_dir):
        res = re.search(r'appdir ?=(.*)\n', content)
        if res:
            prev = res.group(1).strip()
            if prev == appdir:
                logger.info(f'{init_file}: appdir уже исправлен')
            else:
                content = re.sub(r'appdir ?=.*\n', f'appdir = {appdir}\n', content)
                modified = True
                logger.info(f'{init_file}: appdir исправлен')
        else:
            logger.info(f'{init_file}: appdir не найден')
    else:
        logger.info('uiautomator2cache не установлен, исправление пропущено')

    # 保存文件
    if modified:
        with open(init_file, 'w', encoding='utf-8') as f:
            f.write(content)
        logger.info(f'{init_file}: содержимое сохранено')


def patch_apkutils2():
    """移除 adbutils 中对 apkutils2 的导入。

    adbutils/mixin.py 的 ShellMixin.install 导入了 apkutils2，但 apkutils2 不提供 wheel 文件，
    可能因未知原因安装失败。由于我们从不使用该方法，直接移除该导入。
    """
    mixin = site_package_file('adbutils', 'mixin.py')
    if not mixin:
        logger.info('adbutils не установлен, исправление пропущено')
        return

    try:
        with open(mixin, 'r', encoding='utf-8') as f:
            content = f.read()
    except FileNotFoundError:
        logger.info(f'{mixin} не существует')
        return

    res = re.search(r'import apkutils2', content)
    if res:
        content = re.sub(r'import apkutils2', '', content)
        with open(mixin, 'w', encoding='utf-8') as f:
            f.write(content)
        logger.info(f'{mixin}: apkutils2 исправлен')
    else:
        logger.info(f'{mixin}: исправление apkutils2 не требуется')


def pre_checks():
    check_running_directory()

    patch_uiautomator2()
    patch_apkutils2()


if __name__ == '__main__':
    pre_checks()
