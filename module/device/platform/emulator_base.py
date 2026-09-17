"""Определение базовых классов эмуляторов. Предоставляет абстрактные интерфейсы
EmulatorBase, EmulatorInstanceBase, EmulatorManagerBase, определяя общий протокол
управления путями и экземплярами эмуляторов."""

import os
import re
import typing as t
from dataclasses import dataclass

from module.device.platform.utils import cached_property, iter_folder


def abspath(path):
    return os.path.abspath(path).replace('\\', '/')


def get_serial_pair(serial):
    """
    Выводит парный серийный номер по переданному serial.

    Args:
        serial (str): Серийный номер устройства.

    Returns:
        tuple: `127.0.0.1:5555+{X}` и `emulator-5554+{X}`, где 0 <= X <= 32.
    """
    if serial.startswith('127.0.0.1:'):
        try:
            port = int(serial[10:])
            if 5555 <= port <= 5555 + 32:
                return f'127.0.0.1:{port}', f'emulator-{port - 1}'
        except (ValueError, IndexError):
            pass
    if serial.startswith('emulator-'):
        try:
            port = int(serial[9:])
            if 5554 <= port <= 5554 + 32:
                return f'127.0.0.1:{port + 1}', f'emulator-{port}'
        except (ValueError, IndexError):
            pass

    return None, None


def remove_duplicated_path(paths):
    """
    Удаляет дублирующиеся пути без учёта регистра, сохраняя исходный регистр первого вхождения.

    Args:
        paths (list[str]): Список путей.

    Returns:
        list[str]: Список путей без дубликатов.
    """
    paths = sorted(set(paths))
    dic = {}
    for path in paths:
        dic.setdefault(path.lower(), path)
    return list(dic.values())


@dataclass
class EmulatorInstanceBase:
    """Базовая структура данных экземпляра эмулятора."""
    # Серийный номер для подключения ADB
    serial: str
    # Имя экземпляра эмулятора, используется для запуска и остановки
    name: str
    # Путь к .exe-файлу эмулятора
    path: str
    # Дополнительное поле конкретного эмулятора (необязательно)
    index: int = 0
    state: str = ''

    def __str__(self):
        return f'{self.type}(serial="{self.serial}", name="{self.name}", path="{self.path}")'

    @cached_property
    def type(self) -> str:
        """
        Returns:
            str: Тип эмулятора, например Emulator.NoxPlayer.
        """
        return self.emulator.type

    @cached_property
    def emulator(self):
        """
        Returns:
            EmulatorBase: Объект эмулятора, соответствующий текущему экземпляру.
        """
        return EmulatorBase(self.path)

    def __eq__(self, other):
        if isinstance(other, str) and self.type == other:
            return True
        if isinstance(other, list) and self.type in other:
            return True
        if isinstance(other, EmulatorInstanceBase):
            return super().__eq__(other) and self.type == other.type
        return super().__eq__(other)

    def __hash__(self):
        return hash(str(self))

    def __bool__(self):
        return True

    @cached_property
    def MuMuPlayer12_id(self):
        """
        Преобразует имя экземпляра MuMu 12 в идентификатор экземпляра (ID).
        Примеры имён:
            MuMuPlayer-12.0-3
            MuMuPlayerGlobal-12.0-0
            MuMuPlayer-15.0-0
            YXArkNights-12.0-1

        Returns:
            int: Идентификатор экземпляра или None, если это не экземпляр MuMu 12.
        """
        res = re.search(r'MuMuPlayer(?:Global)?-12.0-(\d+)', self.name)
        if res:
            return int(res.group(1))
        res = re.search(r'MuMuPlayer(?:Global)?-15.0-(\d+)', self.name)
        if res:
            return int(res.group(1))
        res = re.search(r'YXArkNights-12.0-(\d+)', self.name)
        if res:
            return int(res.group(1))

        return None

    def mumu_vms_config(self, file):
        """
        Возвращает абсолютный путь к конфигурационному файлу виртуальной машины MuMu.

        Args:
            file (str): Имя файла конфигурации, например customer_config.json.

        Returns:
            str: Абсолютный путь к файлу конфигурации.
        """
        return self.emulator.abspath(f'../vms/{self.name}/configs/{file}')

    @cached_property
    def LDPlayer_id(self):
        """
        Преобразует имя экземпляра эмулятора LDPlayer в идентификатор экземпляра (ID).
        Примеры имён:
            leidian0
            leidian1

        Returns:
            int: Идентификатор экземпляра или None, если это не экземпляр LDPlayer.
        """
        res = re.search(r'leidian(\d+)', self.name)
        if res:
            return int(res.group(1))

        return None


class EmulatorBase:
    """Базовый класс эмулятора, определяющий константы типов и общий интерфейс."""
    # Значения здесь должны совпадать с EmulatorInfo.Emulator.option в argument.yaml
    NoxPlayer = 'NoxPlayer'
    NoxPlayer64 = 'NoxPlayer64'
    NoxPlayerFamily = [NoxPlayer, NoxPlayer64]
    BlueStacks4 = 'BlueStacks4'
    BlueStacks5 = 'BlueStacks5'
    BlueStacks4HyperV = 'BlueStacks4HyperV'
    BlueStacks5HyperV = 'BlueStacks5HyperV'
    BlueStacksFamily = [BlueStacks4, BlueStacks5]
    LDPlayer3 = 'LDPlayer3'
    LDPlayer4 = 'LDPlayer4'
    LDPlayer9 = 'LDPlayer9'
    LDPlayer14 = 'LDPlayer14'
    LDPlayerFamily = [LDPlayer3, LDPlayer4, LDPlayer9, LDPlayer14]
    MuMuPlayer = 'MuMuPlayer'
    MuMuPlayerX = 'MuMuPlayerX'
    MuMuPlayer12 = 'MuMuPlayer12'
    MuMuPlayerFamily = [MuMuPlayer, MuMuPlayerX, MuMuPlayer12]
    MEmuPlayer = 'MEmuPlayer'
    # Эмуляторы для Mac
    BlueStacksAir = 'BlueStacksAir'
    MuMuPro = 'MuMuPro'
    MacEmulatorFamily = [BlueStacksAir, MuMuPro]
    SSH = 'SSH'

    @classmethod
    def path_to_type(cls, path: str) -> str:
        """
        Определяет тип эмулятора по пути к .exe-файлу.

        Args:
            path: Путь к .exe-файлу.

        Returns:
            str: Тип эмулятора, например Emulator.NoxPlayer; пустая строка, если не является эмулятором.
        """
        return ''

    def iter_instances(self) -> t.Iterable[EmulatorInstanceBase]:
        """
        Перебирает все обнаруженные экземпляры текущего эмулятора.

        Yields:
            EmulatorInstanceBase: Экземпляр эмулятора.
        """
        pass

    def iter_adb_binaries(self) -> t.Iterable[str]:
        """
        Перебирает пути к исполняемым файлам adb, найденным в текущем эмуляторе.

        Yields:
            str: Абсолютный путь к исполняемому файлу adb.
        """
        pass

    def __init__(self, path):
        # Путь к .exe-файлу
        self.path = path.replace('\\', '/')
        # Каталог установки эмулятора
        self.dir = os.path.dirname(path)
        # str: тип эмулятора; пустая строка, если это не эмулятор
        self.type = self.__class__.path_to_type(path)

    def __eq__(self, other):
        if isinstance(other, str) and self.type == other:
            return True
        if isinstance(other, list) and self.type in other:
            return True
        return super().__eq__(other)

    def __str__(self):
        return f'{self.type}(path="{self.path}")'

    __repr__ = __str__

    def __hash__(self):
        return hash(self.path)

    def __bool__(self):
        return True

    def abspath(self, path, folder=None):
        if folder is None:
            folder = self.dir
        return abspath(os.path.join(folder, path))

    @classmethod
    def is_emulator(cls, path: str) -> bool:
        """
        Определяет, является ли указанный путь эмулятором.

        Args:
            path: Путь к .exe-файлу.

        Returns:
            bool: Является ли эмулятором.
        """
        return bool(cls.path_to_type(path))

    def list_folder(self, folder, is_dir=False, ext=None):
        """
        Безопасно выводит список файлов в папке.

        Args:
            folder: Путь к папке (относительно каталога эмулятора).
            is_dir: Перечислять ли только каталоги.
            ext: Фильтр по расширению файла.

        Returns:
            list[str]: Список путей к файлам.
        """
        folder = self.abspath(folder)
        return list(iter_folder(folder, is_dir=is_dir, ext=ext))


class EmulatorManagerBase:
    """Базовый класс менеджера эмуляторов, предоставляющий общий интерфейс обнаружения и перечисления эмуляторов."""

    @staticmethod
    def iter_running_emulator():
        """
        Перебирает пути к исполняемым файлам запущенных эмуляторов.

        Yields:
            str: Путь к исполняемому файлу эмулятора, может содержать дубликаты.
        """
        return

    @cached_property
    def all_emulators(self) -> t.List[EmulatorBase]:
        """
        Возвращает все эмуляторы, установленные на текущем компьютере.

        Returns:
            list[EmulatorBase]: Список эмуляторов.
        """
        return []

    @cached_property
    def all_emulator_instances(self) -> t.List[EmulatorInstanceBase]:
        """
        Возвращает все экземпляры эмуляторов, установленные на текущем компьютере.

        Returns:
            list[EmulatorInstanceBase]: Список экземпляров эмуляторов.
        """
        return []

    @cached_property
    def all_emulator_serials(self) -> t.List[str]:
        """
        Возвращает все возможные серийные номера устройств на текущем компьютере.

        Returns:
            list[str]: Список серийных номеров.
        """
        out = []
        for emulator in self.all_emulator_instances:
            out.append(emulator.serial)
            # Также добавляем serial в формате `emulator-5554`
            port_serial, emu_serial = get_serial_pair(emulator.serial)
            if emu_serial:
                out.append(emu_serial)
        return out

    @cached_property
    def all_adb_binaries(self) -> t.List[str]:
        """
        Возвращает пути к исполняемым файлам adb всех эмуляторов на текущем компьютере.

        Returns:
            list[str]: Список путей к исполняемым файлам adb.
        """
        out = []
        for emulator in self.all_emulators:
            for exe in emulator.iter_adb_binaries():
                out.append(exe)
        return out
