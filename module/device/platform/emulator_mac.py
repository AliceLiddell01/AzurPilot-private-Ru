"""macOS 模拟器管理。实现 macOS 平台的模拟器实例检测和管理，
支持 BlueStacks Air 和 MuMu Pro 等模拟器。"""

import json
import os
import re
import subprocess
from dataclasses import dataclass

import psutil

from module.device.platform.emulator_base import EmulatorBase, EmulatorInstanceBase, EmulatorManagerBase
from module.device.platform.utils import cached_property
from module.logger import logger


def abspath(path):
    return os.path.abspath(path).replace('\\', '/')


class EmulatorInstanceMac(EmulatorInstanceBase):
    """macOS 平台的模拟器实例。"""

    @cached_property
    def emulator(self):
        """
        Returns:
            EmulatorMac: 当前实例对应的 Mac 模拟器对象
        """
        return EmulatorMac(self.path)


class EmulatorMac(EmulatorBase):
    """
    macOS 平台的模拟器类型。
    此处的值必须与 argument.yaml 中 EmulatorInfo.Emulator.option 保持一致。
    """
    BlueStacksAir = 'BlueStacksAir'
    BlueStacksMIM = 'BlueStacksMIM'
    MuMuPro = 'MuMuPro'

    @classmethod
    def path_to_type(cls, path: str) -> str:
        """
        根据 .app 包或 .exe 文件路径判断模拟器类型（大小写不敏感）。

        Args:
            path: .app 包或 .exe 文件路径

        Returns:
            str: 模拟器类型，如 EmulatorMac.BlueStacksAir；如果不是模拟器则返回空字符串
        """
        path = path.lower()

        # BlueStacks MIM (на базе Hyper-V)
        if 'bluestacksmim' in path or 'bluestacks_mim' in path:
            return cls.BlueStacksMIM

        # BlueStacks Air
        if 'bluestacks' in path:
            if '/bluestacks.app/' in path or path.endswith('/bluestacks.app'):
                return cls.BlueStacksAir
            # Также проверяем исполняемый файл
            if 'bluestacks' in path and ('hd-player' in path or path.endswith('bluestacks')):
                return cls.BlueStacksAir

        # MuMu Pro (Mac)
        if 'mumu' in path:
            # Проверяем MuMuEmulator (фактический процесс эмулятора)
            if 'mumuemulator' in path or 'mumu' in path and 'emulator' in path:
                return cls.MuMuPro
            # Также проверяем MuMuPlayer (старые версии)
            if '/mumu' in path and 'player' in path:
                return cls.MuMuPro

        return ''

    @staticmethod
    def find_app_bundle(search_name: str, exclude_names: list = None) -> str:
        """
        在 /Applications 目录中查找应用包。

        Args:
            search_name: 要搜索的应用名称（如 "BlueStacks"、"MuMu"）
            exclude_names: 需要排除的名称列表

        Returns:
            str: .app 包的完整路径；未找到则返回空字符串
        """
        apps_dir = '/Applications'
        if not os.path.exists(apps_dir):
            return ''

        if exclude_names is None:
            exclude_names = []

        # Сначала пробуем точное совпадение
        for item in os.listdir(apps_dir):
            if item.lower() == search_name.lower():
                # Проверяем исключения
                excluded = False
                for ex in exclude_names:
                    if item.lower() == ex.lower():
                        excluded = True
                        break
                if not excluded:
                    return os.path.join(apps_dir, item)

        # Затем пробуем совпадение по префиксу (предпочитаем более короткое и простое имя)
        matches = []
        for item in os.listdir(apps_dir):
            if item.lower().startswith(search_name.lower()):
                # Проверяем исключения
                excluded = False
                for ex in exclude_names:
                    if item.lower().startswith(ex.lower()):
                        excluded = True
                        break
                if not excluded:
                    matches.append(item)

        if matches:
            # Возвращаем самое короткое совпадение (скорее всего, это базовое имя)
            return os.path.join(apps_dir, min(matches, key=len))

        return ''

    def iter_instances(self):
        """
        遍历在 Mac 上发现的模拟器实例。

        Yields:
            EmulatorInstanceMac: 模拟器实例
        """
        if self == EmulatorMac.BlueStacksMIM:
            # BlueStacks MIM (Hyper-V) использует порт 5555 + 10*n
            app_path = self.find_app_bundle('BlueStacksMIM')
            if app_path:
                yield EmulatorInstanceMac(
                    serial='127.0.0.1:5555',
                    name='BlueStacksMIM',
                    path=app_path + '/Contents/MacOS/BlueStacks'
                )

        elif self == EmulatorMac.BlueStacksAir:
            # BlueStacks Air обычно использует порт 5555 + 10*n
            # Экземпляр по умолчанию: 127.0.0.1:5555
            # Несколько экземпляров: 127.0.0.1:5555, 5565, 5575 и т. д.
            app_path = self.find_app_bundle('BlueStacks')
            if app_path:
                # Пытаемся получить сведения об экземпляре из файла конфигурации
                config_path = os.path.expanduser('~/Library/Preferences/com.bluestacks.blueStacks.plist')
                if os.path.exists(config_path):
                    try:
                        result = subprocess.run(
                            ['defaults', 'read', config_path, 'bst.instance'],
                            capture_output=True, text=True
                        )
                        if result.returncode == 0 and result.stdout.strip():
                            # Разбираем сведения об экземпляре
                            yield EmulatorInstanceMac(
                                serial='127.0.0.1:5555',
                                name='BlueStacksAir',
                                path=app_path + '/Contents/MacOS/BlueStacks'
                            )
                            return
                    except Exception:
                        pass

                # Экземпляр по умолчанию
                yield EmulatorInstanceMac(
                    serial='127.0.0.1:5555',
                    name='BlueStacksAir',
                    path=app_path + '/Contents/MacOS/BlueStacks'
                )

        elif self == EmulatorMac.MuMuPro:
            # MuMu Pro на macOS
            # Используем mumutool для получения списка экземпляров и сведений о портах
            app_path = self.find_app_bundle('MuMu')
            if app_path:
                mumu_bin_path = os.path.join(app_path, 'Contents/MacOS/mumutool')
                if os.path.exists(mumu_bin_path):
                    try:
                        # Получаем все экземпляры через 'mumutool info all'
                        result = subprocess.run(
                            [mumu_bin_path, 'info', 'all'],
                            capture_output=True,
                            text=True,
                            timeout=10
                        )
                        if result.returncode == 0 and result.stdout.strip():
                            try:
                                data = json.loads(result.stdout)
                                if data.get('errcode') == 0 and 'return' in data:
                                    return_data = data['return']
                                    if 'results' in return_data:
                                        # Несколько экземпляров
                                        devices = return_data['results']
                                    elif 'count' in return_data and return_data['count'] == 1:
                                        # Один экземпляр (вне массива results)
                                        devices = [return_data]
                                    else:
                                        devices = []

                                    for dev in devices:
                                        index = dev.get('index', 0)
                                        # Предпочитаем adb_port, иначе вычисляем порт по index
                                        adb_port = dev.get('adb_port', 16384 + index * 32)
                                        name = dev.get('name', f'MuMuPro-{index}')
                                        state = dev.get('state', 'unknown')
                                        yield EmulatorInstanceMac(
                                            serial=f'127.0.0.1:{adb_port}',
                                            name=name,
                                            path=app_path + '/Contents/MacOS/MuMuEmulator.app/Contents/MacOS/MuMuEmulator',
                                            index=index,
                                            state=state
                                        )
                                    return
                            except json.JSONDecodeError:
                                pass
                    except (subprocess.TimeoutExpired, Exception) as e:
                        logger.debug(f'Не удалось выполнить команду `mumutool info all`: {e}')

                # Fallback: экземпляр по умолчанию
                yield EmulatorInstanceMac(
                    serial='127.0.0.1:16384',
                    name='MuMuPro',
                    path=app_path + '/Contents/MacOS/MuMuEmulator.app/Contents/MacOS/MuMuEmulator',
                    index=0,
                    state='unknown'
                )

    def iter_adb_binaries(self) -> list:
        """
        遍历当前模拟器中找到的 adb 二进制文件路径。

        Yields:
            str: adb 二进制文件的绝对路径
        """
        # Ищем adb в типичных расположениях
        adb_locations = [
            self.abspath('../ADB/adb'),
            self.abspath('../../Android/SDK/platform-tools/adb'),
            '/usr/local/bin/adb',
            '/opt/homebrew/bin/adb',
        ]

        for adb_path in adb_locations:
            if os.path.exists(adb_path):
                yield adb_path

        # Также ищем adb, поставляемый вместе с BlueStacks/MuMu
        if self == EmulatorMac.BlueStacksAir:
            app_path = self.find_app_bundle('BlueStacks')
            if app_path:
                adb_path = os.path.join(app_path, 'Contents/Resources/ADB/adb')
                if os.path.exists(adb_path):
                    yield adb_path

        elif self == EmulatorMac.MuMuPro:
            app_path = self.find_app_bundle('MuMu')
            if app_path:
                adb_path = os.path.join(app_path, 'Contents/MacOS/ADB/adb')
                if os.path.exists(adb_path):
                    yield adb_path


class EmulatorManagerMac(EmulatorManagerBase):
    """macOS 平台的模拟器管理器。"""

    @staticmethod
    def iter_running_emulator():
        """
        遍历正在运行的模拟器可执行文件路径。

        Yields:
            str: 模拟器可执行文件路径，可能包含重复值
        """
        try:
            for proc in psutil.process_iter():
                try:
                    name = proc.info.get('name', '')
                    if not name:
                        continue

                    # Проверяем процессы эмуляторов Mac
                    name_lower = name.lower()
                    if 'bluestacks' in name_lower or 'mumu' in name_lower:
                        # Пытаемся получить фактический путь
                        try:
                            cmdline = proc.cmdline()
                            if cmdline:
                                yield cmdline[0]
                        except (psutil.AccessDenied, psutil.NoSuchProcess):
                            pass
                except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                    continue
        except Exception:
            pass

    @cached_property
    def all_emulators(self) -> list:
        """
        获取当前 Mac 上安装的所有模拟器。

        Returns:
            list[EmulatorMac]: 模拟器列表
        """
        emulators = []

        # Проверяем BlueStacks MIM (Hyper-V)
        app_path = EmulatorMac.find_app_bundle('BlueStacksMIM')
        if app_path:
            exe_path = app_path + '/Contents/MacOS/BlueStacks'
            if os.path.exists(exe_path):
                emulators.append(EmulatorMac(exe_path))

        # Проверяем BlueStacks Air (не MIM)
        app_path = EmulatorMac.find_app_bundle('BlueStacks', exclude_names=['BlueStacksMIM'])
        if app_path:
            exe_path = app_path + '/Contents/MacOS/BlueStacks'
            if os.path.exists(exe_path):
                emulators.append(EmulatorMac(exe_path))

        # Проверяем MuMu Pro
        app_path = EmulatorMac.find_app_bundle('MuMu')
        if app_path:
            # Проверяем MuMuEmulator (фактический процесс эмулятора)
            exe_path = app_path + '/Contents/MacOS/MuMuEmulator.app/Contents/MacOS/MuMuEmulator'
            if os.path.exists(exe_path):
                emulators.append(EmulatorMac(exe_path))
            else:
                # Fallback на MuMuPlayer (старые версии)
                exe_path = app_path + '/Contents/MacOS/MuMuPlayer'
                if os.path.exists(exe_path):
                    emulators.append(EmulatorMac(exe_path))

        return emulators

    @cached_property
    def all_emulator_instances(self) -> list:
        """
        获取当前 Mac 上安装的所有模拟器实例。

        Returns:
            list[EmulatorInstanceMac]: 模拟器实例列表
        """
        instances = []
        for emulator in self.all_emulators:
            instances += list(emulator.iter_instances())
        return sorted(instances, key=lambda x: str(x))


if __name__ == '__main__':
    self = EmulatorManagerMac()
    for emu in self.all_emulator_instances:
        print(emu)
